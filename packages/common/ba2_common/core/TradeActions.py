"""
TradeActions - Core component for executing trading actions

This module provides base classes and implementations for executing various trading actions
based on expert recommendations and market conditions.
"""

import math
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone, date, timedelta

from ba2_common.core.interfaces import AccountInterface
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.models import TradingOrder, ExpertRecommendation, TradeActionResult
from ba2_common.core.types import (
    OrderRecommendation, ExpertActionType, OrderDirection, OrderStatus,
    OptionRight, AssetClass, TransactionStatus,
)
from ba2_common.core.db import get_db, add_instance, update_instance, get_instance
from ba2_common.core.option_economics import (
    ARC_FLOOR_REFUSAL, admits_credit_structure, annualized_return_on_collateral,
)
from ba2_common.core.option_entry_quote import (
    ENTRY_CROSS_NEUTRAL, entry_limit_with_concession, quote_concession,
)
from ba2_common.core.option_payoff import PayoffLeg
from ba2_common.core.option_request import ResolvedStructure
from ba2_common.core.option_types import OptionContract, OptionLeg, OptionPosition
from ba2_common.core.option_selector import (
    select_single, select_vertical_spread, select_wing, passes_liquidity,
    check_liquidity_data_available, OptionDteWindowError, OptionSelectionConfigError,
    OptionLiquidityDataMissingToday)
from ba2_common.logger import logger
from ba2_common.core.failure_modes import absorb_if_benign
from ba2_common.core.db import InstanceNotFound
from ba2_common.core.instance_resolver import InstanceResolverNotConfigured


class TradeAction(ABC):
    """
    Base class for all trading actions.
    
    Provides common functionality for executing trading actions based on:
    - Account interface
    - Instrument information
    - Trade recommendations
    - Existing orders
    """
    
    def __init__(self, instrument_name: str, account: AccountInterface, 
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 expert_recommendation: Optional[ExpertRecommendation] = None):
        """
        Initialize the trade action.
        
        Args:
            instrument_name: Name of the instrument to trade
            account: Account interface for executing trades
            order_recommendation: The recommendation that triggered this action
            existing_order: Optional existing order related to this action
            expert_recommendation: Optional expert recommendation object for linking
        """
        self.instrument_name = instrument_name
        self.account = account
        self.order_recommendation = order_recommendation
        self.existing_order = existing_order
        self.expert_recommendation = expert_recommendation
        # Flag indicating whether orders should be submitted to broker (True) or created as PENDING (False)
        self.submit_to_broker = True
        
    @abstractmethod
    def execute(self) -> "TradeActionResult":
        """
        Execute the trading action.
        
        Returns:
            TradeActionResult object containing execution results including:
            - success: bool indicating if action was successful
            - message: str with status message
            - data: dict with additional data (order ID, etc.)
            - action_type: str indicating the type of action executed
            - timestamps and relationships
        """
        pass
    
    @abstractmethod
    def get_description(self) -> str:
        """
        Get a human-readable description of what this action does.
        
        Returns:
            str: Description of the action
        """
        pass
    
    def get_current_price(self) -> Optional[float]:
        """
        Get current market price for the instrument.
        
        Returns:
            Current price or None if unavailable
        """
        try:
            return self.account.get_instrument_current_price(self.instrument_name)
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error getting current price for {self.instrument_name}: {e}", exc_info=True)
            return None
    
    def get_current_position(self) -> Optional[float]:
        """
        Get current position quantity for the instrument.

        Same tri-state contract as ``TradeConditions.TradeCondition.get_current_position``
        (see there for the full rationale and the 2026-07-03 incident): ``None`` means
        ONLY "the fetch succeeded and this instrument is not held". An unverified book
        raises instead, because the two must not share a value.

        The consumers here (``SellAction``, ``CloseAction``) already fail CLOSED on
        ``None``, so the old swallow was misleading rather than money-losing: an outage
        produced "No long position to sell for AAPL", a confident claim about a book
        nobody managed to read.

        Returns:
            Position quantity (positive long / negative short), or ``None`` when the
            fetch SUCCEEDED and this instrument is not held.

        Raises:
            PositionFetchFailed: the position book is UNVERIFIED.
        """
        from ba2_common.core.portfolio_allocation import PositionFetchFailed

        try:
            positions = self.account.get_positions()
        except Exception as e:
            # See TradeConditions: a transport failure is a genuine runtime condition at
            # this site; a defect still propagates under BA2_ERROR_MODE=enforce.
            absorb_if_benign(e, InstanceNotFound, OSError)
            logger.error(
                f"Position fetch RAISED for {self.instrument_name}: {e} — the account's "
                f"position book is unverified", exc_info=True)
            raise PositionFetchFailed(
                f"get_positions() raised while checking {self.instrument_name}: {e}") from e

        if positions is None:
            logger.error(
                f"Position fetch FAILED for {self.instrument_name}: "
                f"{type(self.account).__name__}.get_positions() returned None "
                f"(fetch failure, NOT a flat account)")
            raise PositionFetchFailed(
                f"get_positions() returned None while checking {self.instrument_name}")

        for position in positions:
            # Dict-shaped books were silently skipped by the old `hasattr(position,
            # 'symbol')` test, which then answered "no position".
            if isinstance(position, dict):
                symbol, qty = position.get('symbol'), position.get('qty')
            else:
                symbol, qty = getattr(position, 'symbol', None), getattr(position, 'qty', None)
            if symbol == self.instrument_name:
                return qty
        return None

    def get_expert_position(self) -> Optional[float]:
        """
        Get the expert's own position quantity for the instrument from transactions.

        Unlike get_current_position() which returns the total broker position
        (shared across all experts), this returns only the quantity belonging
        to the expert that owns this action.

        Returns:
            Signed quantity (positive for long, negative for short), 0 if no
            open transactions, or None if expert_id is unavailable.
        """
        expert_id = self.expert_recommendation.instance_id if self.expert_recommendation else None
        if not expert_id:
            return None
        try:
            from ba2_common.core.types import TransactionStatus
            from ba2_common.core.trade_store import transactions_where

            # transactions_where is the dual-path equivalent of the raw select() this
            # replaced (review 2026-07-18, M2): a raw select(Transaction) silently finds
            # nothing when the backtest in-mem store is active (Transaction is an in-mem
            # model, see trade_store.IN_MEM_MODELS), so get_expert_position always returned
            # 0.0/None in that mode instead of the real position.
            transactions = transactions_where(
                symbol=self.instrument_name, expert_id=expert_id,
                statuses=[TransactionStatus.WAITING, TransactionStatus.OPENED],
            )

            if not transactions:
                return 0.0

            total = 0.0
            for t in transactions:
                qty = abs(float(t.quantity))
                if t.side == OrderDirection.BUY:
                    total += qty
                else:
                    total -= qty
            return total
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error getting expert position for {self.instrument_name}: {e}", exc_info=True)
            return None
    
    def _build_order_data(self, expert_recommendation_id: Optional[int]) -> Optional[Dict[str, Any]]:
        """
        Build order data field by copying expert recommendation data.
        
        If expert recommendation has data, copy it to order.data with expert name as key.
        Never override existing values - store each expert's data separately using expert name as key.
        
        Args:
            expert_recommendation_id: ID of expert recommendation (if any)
            
        Returns:
            Dictionary with structure {"ExpertName": {...expert data...}}, or None if no data
        """
        if not expert_recommendation_id:
            return None
        
        try:
            from ba2_common.core.db import get_instance
            from ba2_common.core.models import ExpertRecommendation
            
            expert_rec = get_instance(ExpertRecommendation, expert_recommendation_id)
            if not expert_rec or not expert_rec.data:
                return None
            
            # Expert recommendation should have data with structure like {"SenateCopy": {...}}
            # Return as-is since it's already keyed by expert name
            return expert_rec.data
            
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.debug(f"Could not copy data from expert recommendation {expert_recommendation_id}: {e}")
            return None
    
    def create_order_record(self, side: str, quantity: float, order_type: str = "market",
                          limit_price: Optional[float] = None, stop_price: Optional[float] = None,
                          linked_order_id: Optional[int] = None,
                          extra_data: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """
        Create (and PERSIST) a TradingOrder database record.

        Args:
            side: Order side ("buy" or "sell", case-insensitive)
            quantity: Order quantity
            order_type: Order type ("market", "limit", "stop", etc.)
            limit_price: Limit price for limit orders
            stop_price: Stop price for stop orders
            linked_order_id: ID of linked order (for TP/SL orders)
            extra_data: Optional keys merged into order.data (e.g. {"lot_size": 100}
                so the risk manager sizes the order in round lots)

        Returns:
            The integer id of the saved TradingOrder, or None if creation failed.

            An INT, not the object: the row is already committed and a detached
            instance would raise DetachedInstanceError on attribute access (see the
            add_instance call at the end of this method). This was annotated
            ``Optional[TradingOrder]`` while returning an int, and both share
            actions read the annotation, fed the int back into ``add_instance()``
            and died with "Class 'builtins.int' is not mapped" on every single
            invocation. Callers want ``order_id = self.create_order_record(...)``.
        """
        try:
            # Convert side to uppercase to match OrderDirection enum values (BUY, SELL)
            side_upper = side.upper()
            
            # Build comment string with ACC/TR/REC format
            # [ACC:1/TR:3/REC:5] where ACC=account_id, TR=expert_instance_id, REC=expert_recommendation_id
            comment_parts = [f"ACC:{self.account.id}"]
            expert_instance_id = None
            expert_recommendation_id = None
            
            # First try to get expert recommendation from self.expert_recommendation (for BUY/SELL/CLOSE actions)
            if self.expert_recommendation:
                expert_instance_id = self.expert_recommendation.instance_id
                expert_recommendation_id = self.expert_recommendation.id
                comment_parts.append(f"TR:{expert_instance_id}")
                comment_parts.append(f"REC:{expert_recommendation_id}")
            # For TP/SL orders, copy from existing_order if no expert_recommendation
            elif self.existing_order and self.existing_order.expert_recommendation_id:
                expert_recommendation_id = self.existing_order.expert_recommendation_id
                # Get expert instance ID from the recommendation
                from ba2_common.core.db import get_instance
                from ba2_common.core.models import ExpertRecommendation
                expert_rec = get_instance(ExpertRecommendation, expert_recommendation_id)
                if expert_rec:
                    expert_instance_id = expert_rec.instance_id
                    comment_parts.append(f"TR:{expert_instance_id}")
                    comment_parts.append(f"REC:{expert_recommendation_id}")
            
            comment = f"[{'/'.join(comment_parts)}]"
            
            # Determine open_type: AUTOMATIC for TP/SL orders, otherwise from expert_recommendation presence
            from ba2_common.core.types import OrderOpenType
            if linked_order_id is not None:
                # This is a TP/SL order (has a linked parent order)
                open_type = OrderOpenType.AUTOMATIC
            elif expert_recommendation_id is not None:
                # Order created from expert recommendation
                open_type = OrderOpenType.AUTOMATIC
            else:
                # Manual order
                open_type = OrderOpenType.MANUAL
            
            order_data = self._build_order_data(expert_recommendation_id)  # Copy expert recommendation data
            if extra_data:
                order_data = {**(order_data or {}), **extra_data}

            order = TradingOrder(
                account_id=self.account.id,
                symbol=self.instrument_name,
                side=side_upper,
                quantity=quantity,
                order_type=order_type,
                limit_price=limit_price,
                stop_price=stop_price,
                status=OrderStatus.PENDING.value,
                # parent_order_id is the model's field for "parent OCO order if
                # this is a leg"; linked_order_id does not exist and was dropped,
                # so a TP/SL order's link to its parent was silently lost.
                parent_order_id=linked_order_id,
                expert_recommendation_id=expert_recommendation_id,
                open_type=open_type,
                comment=comment,
                created_at=datetime.now(timezone.utc),
                data=order_data
            )
            
            order_id = add_instance(order)
            if order_id:
                # Return the order_id directly instead of the detached order object
                # This prevents DetachedInstanceError when accessing the id later
                return order_id
            else:
                logger.error("Failed to create order record in database")
                return None
                
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error creating order record: {e}", exc_info=True)
            return None
    
    def create_and_save_action_result(self, action_type: str, success: bool, message: str,
                                       data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Create and save a TradeActionResult, returning a dictionary of attributes.
        
        This method creates a result, saves it to the database, and returns a dict
        to avoid DetachedInstanceError when accessing attributes.
        
        Args:
            action_type: Type of action (buy, sell, close, etc.)
            success: Whether the action was successful
            message: Human-readable message about the result
            data: Additional data dictionary
            
        Returns:
            Dictionary with result attributes (id, action_type, success, message, data)
        """
        if data is None:
            data = {}
        
        # Get expert_recommendation_id from self.expert_recommendation if available
        expert_recommendation_id = None
        if self.expert_recommendation:
            expert_recommendation_id = self.expert_recommendation.id
        
        # If this action has evaluation_details attached (from live execution), include them
        if hasattr(self, 'evaluation_details') and self.evaluation_details:
            data['evaluation_details'] = self.evaluation_details
            logger.debug(f"Storing evaluation details in TradeActionResult for {action_type}")
        
        # Add calculation preview for TP/SL actions if available
        if hasattr(self, 'get_calculation_preview'):
            try:
                calc_preview = self.get_calculation_preview()
                data['calculation_preview'] = calc_preview
                logger.debug(f"Storing calculation preview in TradeActionResult for {action_type}")
            except Exception as e:
                absorb_if_benign(e, InstanceNotFound)
                logger.debug(f"Could not get calculation preview: {e}")
        
        # Create the result object (only if we have expert_recommendation_id)
        if not expert_recommendation_id:
            logger.warning(f"Creating TradeActionResult without expert_recommendation_id for {action_type}")
            # For backward compatibility during migration, allow creation without it
            # TODO: Make expert_recommendation_id required after migration
        
        result = TradeActionResult(
            action_type=action_type,
            success=success,
            message=message,
            data=data,
            expert_recommendation_id=expert_recommendation_id
        )
        
        # Save to database (this closes the session, detaching the object)
        result_id = add_instance(result)
        
        # Return a dictionary instead of the detached object to avoid DetachedInstanceError
        return {
            'id': result_id,
            'action_type': action_type,
            'success': success,
            'message': message,
            'data': data,
            'expert_recommendation_id': expert_recommendation_id
        }


class SellAction(TradeAction):
    """Create a pending sell order for risk management review."""
    
    def execute(self) -> "TradeActionResult":
        """
        Create a pending sell order for the instrument.
        The RiskManager will review, set quantity, and submit the order.
        
        Returns:
            TradeActionResult object containing execution results
        """
        from ba2_common.core.portfolio_allocation import PositionFetchFailed

        try:
            # Get current position to validate we can sell
            try:
                current_position = self.get_current_position()
            except PositionFetchFailed as e:
                # Refuse, exactly as for a confirmed-flat book — but SAY which one it was.
                # "No long position to sell" would be a claim about a book nobody read.
                logger.error(f"SellAction refusing {self.instrument_name}: {e}")
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.SELL.value,
                    success=False,
                    message=(f"Position book unverified for {self.instrument_name} "
                             f"(broker position fetch failed) - refusing to sell"),
                    data={"position_fetch_failed": True}
                )
            if current_position is None or current_position <= 0:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.SELL.value,
                    success=False,
                    message=f"No long position to sell for {self.instrument_name}",
                    data={}
                )

            # Create PENDING order record with quantity=0 (to be set by risk management)
            # Risk management will determine the actual quantity to sell
            order_id = self.create_order_record(
                side="sell",
                quantity=0.0,  # 0 indicates pending review by risk management
                order_type="market"
            )
            
            if not order_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.SELL.value,
                    success=False,
                    message="Failed to create order record",
                    data={}
                )
            
            # Order stays in PENDING status for risk management review
            # RiskManager will call account.submit_order() after setting quantity
            logger.info(f"Created PENDING sell order {order_id} for {self.instrument_name} - awaiting risk management review")
            
            return self.create_and_save_action_result(
                action_type=ExpertActionType.SELL.value,
                success=True,
                message=f"Sell order created for {self.instrument_name} (pending risk management review)",
                data={"order_id": order_id, "status": "PENDING"}
            )
                
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error creating sell order for {self.instrument_name}: {e}", exc_info=True)
            return self.create_and_save_action_result(
                action_type=ExpertActionType.SELL.value,
                success=False,
                message=f"Error creating sell order: {str(e)}",
                data={}
            )
    
    def get_description(self) -> str:
        """Get description of sell action."""
        return f"Create pending sell order for {self.instrument_name} (awaiting risk management review)"


class BuyAction(TradeAction):
    """Create a pending buy order for risk management review."""

    def __init__(self, instrument_name: str, account: AccountInterface,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 expert_recommendation: Optional[ExpertRecommendation] = None,
                 lot_size: Optional[int] = None):
        super().__init__(instrument_name, account, order_recommendation, existing_order, expert_recommendation)
        # Optional round-lot constraint: the risk manager sizes the order in
        # multiples of lot_size and rejects it when not even one lot is fundable.
        # Used by option-overlay strategies (covered call / protective put) that
        # need 100-share equity blocks per contract.
        self.lot_size = lot_size

    def execute(self, quantity: Optional[float] = None) -> "TradeActionResult":
        """
        Create a pending buy order for the instrument.
        The RiskManager will review, set quantity, and submit the order.
        
        Args:
            quantity: Optional quantity to buy. If not provided, will be set to 0 (pending review)
            
        Returns:
            TradeActionResult object containing execution results
        """
        try:
            # Create PENDING order with quantity=0 (to be determined by risk management)
            # Risk management will calculate quantity based on:
            # - Available buying power
            # - Risk management rules
            # - Position sizing strategies
            if quantity is None:
                quantity = 0.0  # 0 indicates pending review by risk management
            
            current_price = self.get_current_price()
            if current_price is None:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.BUY.value,
                    success=False,
                    message=f"Cannot get current price for {self.instrument_name}",
                    data={}
                )
            
            # Create PENDING order record (not submitted to broker yet)
            order_id = self.create_order_record(
                side="buy",
                quantity=quantity,
                order_type="market",
                extra_data={"lot_size": int(self.lot_size)} if self.lot_size else None
            )
            
            if not order_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.BUY.value,
                    success=False,
                    message="Failed to create order record",
                    data={}
                )
            
            # Order stays in PENDING status for risk management review
            # RiskManager will call account.submit_order() after setting quantity
            logger.info(f"Created PENDING buy order {order_id} for {self.instrument_name} - awaiting risk management review")
            
            return self.create_and_save_action_result(
                action_type=ExpertActionType.BUY.value,
                success=True,
                message=f"Buy order created for {self.instrument_name} (pending risk management review)",
                data={"order_id": order_id, "status": "PENDING"}
            )
                
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error creating buy order for {self.instrument_name}: {e}", exc_info=True)
            return self.create_and_save_action_result(
                action_type=ExpertActionType.BUY.value,
                success=False,
                message=f"Error creating buy order: {str(e)}",
                data={}
            )
    
    def get_description(self) -> str:
        """Get description of buy action."""
        return f"Create pending buy order for {self.instrument_name} (awaiting risk management review)"


class CloseAction(TradeAction):
    """Close existing position (buy to cover short or sell long position)."""

    def execute(self) -> "TradeActionResult":
        """
        Close the existing position for the instrument.

        When an existing_order with a transaction_id is available (open_positions
        use case), delegates to AccountInterface.close_transaction() which:
        - Uses transaction.quantity (correct per-expert qty, not broker total)
        - Passes is_closing_order=True to bypass hedging checks
        - Handles existing close orders, ERROR retries, WAITING_TRIGGER cleanup

        Returns:
            TradeActionResult object containing execution results
        """
        try:
            # Preferred path: delegate to close_transaction() when we have a transaction
            if self.existing_order and self.existing_order.transaction_id:
                transaction_id = self.existing_order.transaction_id

                if not self.submit_to_broker:
                    logger.info(
                        f"CloseAction: automated trade modification disabled — "
                        f"skipping close_transaction({transaction_id}) for {self.instrument_name}"
                    )
                    return self.create_and_save_action_result(
                        action_type=ExpertActionType.CLOSE.value,
                        success=True,
                        message=f"Close action deferred for {self.instrument_name} (awaiting manual review)",
                        data={"transaction_id": transaction_id, "status": "PENDING"}
                    )

                logger.info(
                    f"CloseAction: delegating to close_transaction({transaction_id}) "
                    f"for {self.instrument_name}"
                )
                result = self.account.close_transaction(transaction_id)

                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE.value,
                    success=result.get("success", False),
                    message=result.get("message", "Unknown result"),
                    data={
                        "transaction_id": transaction_id,
                        "close_order_id": result.get("close_order_id"),
                        "canceled_count": result.get("canceled_count", 0),
                        "deleted_count": result.get("deleted_count", 0),
                    }
                )

            # Fallback: no transaction context — use broker position (legacy path)
            from ba2_common.core.portfolio_allocation import PositionFetchFailed
            try:
                current_position = self.get_current_position()
            except PositionFetchFailed as e:
                # Refuse, as for a confirmed-flat book — but do not claim the book is empty.
                logger.error(f"CloseAction refusing {self.instrument_name}: {e}")
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE.value,
                    success=False,
                    message=(f"Position book unverified for {self.instrument_name} "
                             f"(broker position fetch failed) - refusing to close"),
                    data={"position_fetch_failed": True}
                )
            if current_position is None or current_position == 0:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE.value,
                    success=False,
                    message=f"No position to close for {self.instrument_name}",
                    data={}
                )
            side = "sell" if current_position > 0 else "buy"
            quantity = abs(current_position)

            order_id = self.create_order_record(
                side=side,
                quantity=quantity,
                order_type="market"
            )
            if not order_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE.value,
                    success=False,
                    message="Failed to create order record",
                    data={}
                )

            order_record = get_instance(TradingOrder, order_id)
            if not order_record:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE.value,
                    success=False,
                    message="Failed to retrieve order record",
                    data={}
                )

            if not self.submit_to_broker:
                logger.info(f"Automated trade modification disabled - leaving order {order_id} in PENDING state for manual review")
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE.value,
                    success=True,
                    message=f"Close order created in PENDING state for {self.instrument_name} (awaiting manual review)",
                    data={"order_id": order_id, "status": "PENDING"}
                )

            submit_result = self.account.submit_order(order_record, is_closing_order=True)

            if submit_result is not None:
                if hasattr(submit_result, 'account_order_id') and submit_result.account_order_id:
                    new_broker_id = str(submit_result.account_order_id)
                    if order_record.broker_order_id and order_record.broker_order_id != new_broker_id:
                        logger.warning(
                            f"Order {order_record.id} already has broker_order_id={order_record.broker_order_id}, "
                            f"not overwriting with: {new_broker_id}"
                        )
                    else:
                        order_record.broker_order_id = new_broker_id
                    order_record.status = OrderStatus.OPEN.value
                    update_instance(order_record)

                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE.value,
                    success=True,
                    message=f"Close order submitted for {self.instrument_name}",
                    data={"order_id": order_id, "broker_order_id": getattr(submit_result, 'account_order_id', None)}
                )
            else:
                order_record.status = OrderStatus.CANCELED.value
                update_instance(order_record)
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE.value,
                    success=False,
                    message=f"Failed to submit close order",
                    data={}
                )

        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error executing close action for {self.instrument_name}: {e}", exc_info=True)
            return self.create_and_save_action_result(
                action_type=ExpertActionType.CLOSE.value,
                success=False,
                message=f"Error executing close action: {str(e)}",
                data={}
            )
    
    def get_description(self) -> str:
        """Get description of close action."""
        return f"Close existing position for {self.instrument_name} (sell long or buy to cover short)"


def resolve_min_take_profit_pct(expert_recommendation_id: Optional[int]) -> float:
    """Look up min_take_profit_percent from an ExpertRecommendation id, defaulting to 2.0 when
    unset, the recommendation can't be found, or the lookup itself fails (get_instance raises
    on a missing row rather than returning None) -- this is a best-effort default resolver, so a
    dangling/deleted recommendation id must never propagate as an exception. Standalone
    (ID-only) counterpart to AdjustTakeProfitAction._resolve_min_take_profit_pct, for callers
    with no in-memory ExpertRecommendation object -- e.g. TradeManager re-checking the floor
    post-fill, where only the parent order's expert_recommendation_id is available."""
    if not expert_recommendation_id:
        return 2.0
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import ExpertRecommendation
    try:
        rec = get_instance(ExpertRecommendation, expert_recommendation_id)
    except Exception:
        return 2.0
    return float(getattr(rec, "min_take_profit_percent", None) or 2.0) if rec else 2.0


def compute_tp_floor_price(
    target_price: float, entry_price: float, min_pct: float, is_long: bool
) -> Optional[float]:
    """If `target_price` is closer to `entry_price` than `min_pct`% allows, return the
    floor-enforced price; otherwise None (no adjustment needed). Pure, no I/O -- shared by the
    pre-fill Phase-2 enforcement (AdjustTakeProfitAction._enforce_minimum_distance / compute_price)
    and TradeManager's post-fill re-check of the same floor against the REAL fill price."""
    if not entry_price:
        return None
    if is_long:
        actual_pct = ((target_price - entry_price) / entry_price) * 100
        if actual_pct < min_pct:
            return entry_price * (1 + min_pct / 100)
    else:
        actual_pct = ((entry_price - target_price) / entry_price) * 100
        if actual_pct < min_pct:
            return entry_price * (1 - min_pct / 100)
    return None


class _AdjustPriceLevelAction(TradeAction):
    """
    Base class for TP and SL adjustment actions.

    Subclasses provide the handful of properties and hooks that differ between
    take-profit and stop-loss adjustments; all shared calculation, broker
    interaction, and persistence logic lives here.
    """

    # --- Subclass-provided class attributes ---
    _action_type: str          # e.g. ExpertActionType.ADJUST_TAKE_PROFIT.value
    _label: str                # Short label for log messages ("TP" / "SL")
    _long_label: str           # Human label ("Take profit" / "Stop loss")
    _price_key_prefix: str     # Key prefix for order.data ("tp" / "sl")
    _result_price_key: str     # Key in result data ("new_tp_price" / "new_sl_price")

    def __init__(self, instrument_name: str, account: AccountInterface,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 expert_recommendation: Optional[ExpertRecommendation] = None,
                 target_price: Optional[float] = None,
                 reference_value: Optional[str] = None, percent: Optional[float] = None):
        super().__init__(instrument_name, account, order_recommendation, existing_order, expert_recommendation)
        self.target_price = target_price
        self.reference_value = reference_value
        self.percent = percent

    # --- Regime overlay -------------------------------------------------------------------
    # Lives on the SHARED base, not on a mixin, because there are TWO percent->price sites and
    # both must scale: execute() (single TP-or-SL adjustment) and compute_price() (the merged
    # TP+SL path TradeActionEvaluator uses for entry brackets -- the common case in backtests).
    # A mixin on the subclasses only would have covered execute(), leaving the dominant path
    # unscaled: the ATR failure mode this feature exists to avoid repeating.

    _regime_scale_setting: str = ""      # subclass names its gene; "" -> never scaled

    def _regime_scaled_percent(self) -> float:
        """``self.percent`` after the regime multiplier, or unchanged when it does not apply."""
        if not self._regime_scale_setting or self.percent is None:
            return self.percent
        from ba2_common.core.regime_overlay import get_stressed, regime_scale, scale_percent

        # Cheap gate FIRST. Resolving the expert costs a DB-backed get_expert_id() and this runs
        # per order per bar, so short-circuit on the process-global flag before touching it: on an
        # unstressed bar (and on every run with no regime published at all) nothing can scale, so
        # the lookup would be pure waste.
        stressed = get_stressed()
        if stressed is not True:
            return self.percent

        expert = self._regime_expert()
        scale = regime_scale(expert, self._regime_scale_setting, stressed)
        if scale == 1.0:
            return self.percent          # exact no-op: do not even log
        scaled = scale_percent(self.percent, scale)
        logger.info(
            f"{self._label} regime overlay: stressed market -> {self._regime_scale_setting}"
            f"={scale:g}, offset {self.percent:+.2f}% -> {scaled:+.2f}%")
        return scaled

    def _regime_expert(self):
        """The expert instance whose genome carries the regime settings, or None.

        TP/SL actions are usually constructed WITHOUT an expert_recommendation (the ruleset
        adjusts an order that already exists -- see _create_order's "copy from existing_order"
        branch), so the recommendation is only the first of two paths; ``get_expert_id()`` is the
        canonical fallback and already handles both the transaction and recommendation linkage
        under the backtest in-mem store.

        Returns None -> neutral scale. A missing expert must degrade to today's behaviour, never
        to a guessed multiplier.
        """
        try:
            expert_id = self.expert_recommendation.instance_id if self.expert_recommendation else None
            if expert_id is None and self.existing_order is not None:
                # getattr, not a direct call: an option/stub order object need not implement the
                # full TradingOrder surface, and a missing linkage must read as "no expert".
                getter = getattr(self.existing_order, "get_expert_id", None)
                expert_id = getter() if callable(getter) else None
            if not expert_id:
                return None
            from ba2_common.core.instance_resolver import get_instance_resolver
            return get_instance_resolver().get_expert_instance(expert_id)
        except Exception as e:  # the overlay must never break order placement
            absorb_if_benign(e, InstanceNotFound, InstanceResolverNotConfigured)
            logger.debug(f"{self._label} regime overlay: no expert resolved ({e}); using neutral scale")
            return None

    # --- Hooks that subclasses override ---

    def _call_broker(self, transaction) -> bool:
        """Call the appropriate account method (adjust_tp or adjust_sl)."""
        raise NotImplementedError

    def _post_broker_hook(self, transaction) -> None:
        """Optional post-broker work (e.g. TP stores current_target_price metadata)."""
        pass

    def _enforce_minimum_distance(self) -> None:
        """Optional enforcement of minimum distance from open price (SL only)."""
        pass

    # --- Shared implementation ---

    def execute(self) -> "TradeActionResult":
        """Adjust the price level for existing order using account's adjust method."""
        try:
            if not self.existing_order:
                return self.create_and_save_action_result(
                    action_type=self._action_type,
                    success=False,
                    message=f"No existing order provided for {self._long_label.lower()} adjustment",
                    data={}
                )

            # Calculate price if not directly provided
            if self.target_price is None:
                if self.reference_value is None or self.percent is None:
                    logger.error(f"No {self._long_label.lower()} price, reference_value, or percent provided for {self.instrument_name}")
                    return self.create_and_save_action_result(
                        action_type=self._action_type,
                        success=False,
                        message=f"Missing required parameters: {self._long_label.lower()} price or (reference_value + percent)",
                        data={}
                    )

                logger.info(f"{self._label} Calculation START for {self.instrument_name} - Order ID: {self.existing_order.id}, Side: {self.existing_order.side.upper()}, reference_value: {self.reference_value}, percent: {self.percent:+.2f}%")

                # Get reference price based on reference_value type
                from ba2_common.core.types import ReferenceValue
                reference_price = None

                if self.reference_value == ReferenceValue.ORDER_OPEN_PRICE.value:
                    reference_price = self.existing_order.limit_price

                    if reference_price is None:
                        reference_price = self.existing_order.open_price
                        if reference_price:
                            logger.info(f"{self._label} Reference: ORDER_OPEN_PRICE = ${reference_price:.2f} (from order.open_price - filled order)")
                        else:
                            logger.warning(f"Order {self.existing_order.id} is a market order with no filled price yet, falling back to current market price")
                            reference_price = self.get_current_price()
                            if reference_price:
                                logger.info(f"{self._label} Reference: ORDER_OPEN_PRICE -> CURRENT_PRICE = ${reference_price:.2f} (market order fallback)")
                            else:
                                logger.error(f"Cannot get current price for {self.instrument_name}")
                                return self.create_and_save_action_result(
                                    action_type=self._action_type,
                                    success=False,
                                    message=f"Cannot determine reference price for market order - no filled price or current market price available",
                                    data={}
                                )
                    else:
                        logger.info(f"{self._label} Reference: ORDER_OPEN_PRICE = ${reference_price:.2f} (from order.limit_price)")

                elif self.reference_value == ReferenceValue.CURRENT_PRICE.value:
                    reference_price = self.get_current_price()
                    if reference_price is None:
                        logger.error(f"Cannot get current price for {self.instrument_name}")
                        return self.create_and_save_action_result(
                            action_type=self._action_type,
                            success=False,
                            message=f"Cannot get current market price for {self.instrument_name}",
                            data={}
                        )
                    logger.info(f"{self._label} Reference: CURRENT_PRICE = ${reference_price:.2f} (from market data)")

                elif self.reference_value == ReferenceValue.EXPERT_TARGET_PRICE.value:
                    if self.existing_order and self.existing_order.expert_recommendation_id:
                        from ba2_common.core.db import get_instance
                        from ba2_common.core.models import ExpertRecommendation
                        expert_rec = get_instance(ExpertRecommendation, self.existing_order.expert_recommendation_id)
                        if expert_rec and hasattr(expert_rec, 'price_at_date') and hasattr(expert_rec, 'expected_profit_percent'):
                            base_price = expert_rec.price_at_date
                            expected_profit = expert_rec.expected_profit_percent

                            logger.info(f"{self._label} Reference: EXPERT_TARGET_PRICE - base_price: ${base_price:.2f}, expected_profit: {expected_profit:.1f}%, action: {expert_rec.recommended_action}")

                            if expert_rec.recommended_action in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                                reference_price = base_price * (1 + expected_profit / 100)
                                logger.info(f"{self._label} Target (BUY): ${base_price:.2f} * (1 + {expected_profit:.1f}/100) = ${reference_price:.2f}")
                            elif expert_rec.recommended_action in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                                reference_price = base_price * (1 - expected_profit / 100)
                                logger.info(f"{self._label} Target (SELL): ${base_price:.2f} * (1 - {expected_profit:.1f}/100) = ${reference_price:.2f}")
                            else:
                                logger.error(f"Invalid recommendation action: {expert_rec.recommended_action}")
                                return self.create_and_save_action_result(
                                    action_type=self._action_type,
                                    success=False,
                                    message=f"Invalid recommendation action: {expert_rec.recommended_action}",
                                    data={}
                                )
                        else:
                            logger.error(f"Cannot get expert target price for order {self.existing_order.id} - missing price_at_date or expected_profit_percent")
                            return self.create_and_save_action_result(
                                action_type=self._action_type,
                                success=False,
                                message="Cannot get expert target price from recommendation",
                                data={}
                            )
                    else:
                        logger.error(f"No expert recommendation linked to order {self.existing_order.id}")
                        return self.create_and_save_action_result(
                            action_type=self._action_type,
                            success=False,
                            message="No expert recommendation available for target price",
                            data={}
                        )
                else:
                    logger.error(f"Unknown reference_value: {self.reference_value}")
                    return self.create_and_save_action_result(
                        action_type=self._action_type,
                        success=False,
                        message=f"Unknown reference_value: {self.reference_value}",
                        data={}
                    )

                # Determine position direction
                is_long_position = False
                if self.order_recommendation in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                    is_long_position = True
                    logger.info(f"{self._label} Direction: Using order_recommendation={self.order_recommendation.value} -> LONG position")
                elif self.order_recommendation in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                    is_long_position = False
                    logger.info(f"{self._label} Direction: Using order_recommendation={self.order_recommendation.value} -> SHORT position")
                elif self.existing_order:
                    is_long_position = (self.existing_order.side.upper() == "BUY")
                    logger.info(f"{self._label} Direction: Using existing_order.side={self.existing_order.side.upper()} -> {'LONG' if is_long_position else 'SHORT'} position")
                else:
                    logger.error(f"Cannot determine order direction for {self._label} calculation")
                    return self.create_and_save_action_result(
                        action_type=self._action_type,
                        success=False,
                        message=f"Cannot determine order direction for {self._label} calculation",
                        data={}
                    )

                # REGIME OVERLAY: scale the percent OFFSET (not the price) while the benchmark is
                # stressed. This is the single point where a configured percent becomes a price,
                # for BOTH take-profit and stop-loss, so the multiplier lands here once instead of
                # in each action subclass. Neutral (1.0) unless the genome enabled the overlay AND
                # the bar is stressed, so every persisted ruleset is bit-for-bit unchanged.
                eff_percent = self._regime_scaled_percent()

                # Apply price calculation based on position direction
                if is_long_position:
                    self.target_price = reference_price * (1 + eff_percent / 100)
                    logger.info(f"{self._label} Final (LONG/BUY): ${reference_price:.2f} * (1 + {eff_percent:+.2f}/100) = ${self.target_price:.2f}")
                else:
                    self.target_price = reference_price * (1 - eff_percent / 100)
                    logger.info(f"{self._label} Final (SHORT/SELL): ${reference_price:.2f} * (1 - {eff_percent:+.2f}/100) = ${self.target_price:.2f}")

                logger.info(f"{self._label} Calculation COMPLETE for {self.instrument_name} - Final {self._label} Price: ${self.target_price:.2f}")

            # Subclass hook: enforce minimum distance (SL overrides this)
            self._enforce_minimum_distance()

            # Call broker to adjust the price level
            try:
                if not self.existing_order.transaction_id:
                    logger.error(f"Order {self.existing_order.id} has no linked transaction")
                    return self.create_and_save_action_result(
                        action_type=self._action_type,
                        success=False,
                        message=f"Order {self.existing_order.id} has no linked transaction",
                        data={}
                    )

                from ba2_common.core.models import Transaction
                from ba2_common.core.db import get_instance
                transaction = get_instance(Transaction, self.existing_order.transaction_id)
                if not transaction:
                    logger.error(f"Transaction {self.existing_order.transaction_id} not found")
                    return self.create_and_save_action_result(
                        action_type=self._action_type,
                        success=False,
                        message=f"Transaction {self.existing_order.transaction_id} not found",
                        data={}
                    )

                logger.debug(f"Calling {self._label.lower()} adjustment for transaction {transaction.id} with price ${self.target_price:.2f}")
                success = self._call_broker(transaction)

                if success:
                    logger.info(f"Successfully adjusted {self._long_label.lower()} for {self.instrument_name}: OCO/OTO order created/updated")

                    # Subclass hook: post-broker work (TP stores metadata)
                    self._post_broker_hook(transaction)
                else:
                    logger.warning(f"Failed to adjust {self._long_label.lower()} for {self.instrument_name}")
                    return self.create_and_save_action_result(
                        action_type=self._action_type,
                        success=False,
                        message=f"Failed to adjust {self._long_label.lower()} for {self.instrument_name}",
                        data={"order_id": self.existing_order.id}
                    )

                # Store percent target in order.data if reference is ORDER_OPEN_PRICE
                if self.reference_value and self.percent is not None and self.existing_order:
                    from ba2_common.core.types import ReferenceValue
                    if self.reference_value == ReferenceValue.ORDER_OPEN_PRICE.value:
                        if not self.existing_order.data:
                            self.existing_order.data = {}

                        self.existing_order.data[f'{self._price_key_prefix}_percent_target'] = round(self.percent, 2)
                        self.existing_order.data[f'{self._price_key_prefix}_reference_type'] = self.reference_value
                        self.existing_order.data[f'{self._price_key_prefix}_reference_price'] = round(self.existing_order.open_price, 2) if self.existing_order.open_price else None

                        update_instance(self.existing_order)
                        logger.info(f"Stored {self._label} percent target: {self.percent:.2f}% (reference: {self.reference_value}) in order {self.existing_order.id}")

                return self.create_and_save_action_result(
                    action_type=self._action_type,
                    success=True,
                    message=f"{self._long_label} adjusted for {self.instrument_name} to ${self.target_price:.2f}",
                    data={
                        "order_id": self.existing_order.id,
                        "transaction_id": transaction.id,
                        self._result_price_key: self.target_price
                    }
                )
            except Exception as set_error:
                logger.error(f"Failed to set {self._long_label.lower()} for order {self.existing_order.id}: {set_error}", exc_info=True)
                raise

        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error adjusting {self._long_label.lower()} for {self.instrument_name}: {e}", exc_info=True)
            return self.create_and_save_action_result(
                action_type=self._action_type,
                success=False,
                message=f"Error adjusting {self._long_label.lower()}: {str(e)}",
                data={"order_id": self.existing_order.id if self.existing_order else None}
            )

    def compute_price(self, order: "TradingOrder") -> Optional[float]:
        """Calculate the price for the given order without submitting to broker."""
        if self.target_price is not None:
            return self.target_price

        if self.reference_value is None or self.percent is None:
            return None

        from ba2_common.core.types import ReferenceValue
        reference_price = None

        if self.reference_value == ReferenceValue.ORDER_OPEN_PRICE.value:
            reference_price = order.limit_price or order.open_price
            if reference_price is None:
                reference_price = self.get_current_price()
        elif self.reference_value == ReferenceValue.CURRENT_PRICE.value:
            reference_price = self.get_current_price()
        elif self.reference_value == ReferenceValue.EXPERT_TARGET_PRICE.value:
            if order and order.expert_recommendation_id:
                from ba2_common.core.db import get_instance
                from ba2_common.core.models import ExpertRecommendation
                expert_rec = get_instance(ExpertRecommendation, order.expert_recommendation_id)
                if expert_rec and hasattr(expert_rec, 'price_at_date') and hasattr(expert_rec, 'expected_profit_percent'):
                    base_price = expert_rec.price_at_date
                    expected_profit = expert_rec.expected_profit_percent
                    if expert_rec.recommended_action in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                        reference_price = base_price * (1 + expected_profit / 100)
                    elif expert_rec.recommended_action in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                        reference_price = base_price * (1 - expected_profit / 100)

        if reference_price is None:
            return None

        if self.order_recommendation in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
            is_long = True
        elif self.order_recommendation in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
            is_long = False
        else:
            order_side = str(order.side.value if hasattr(order.side, 'value') else order.side).upper()
            is_long = (order_side == "BUY")

        eff_percent = self._regime_scaled_percent()
        if is_long:
            return reference_price * (1 + eff_percent / 100)
        else:
            return reference_price * (1 - eff_percent / 100)

    def get_description(self) -> str:
        """Get description of the action."""
        price_desc = f" at ${self.target_price}" if self.target_price else " (auto-calculated)"
        return f"Set or adjust {self._long_label.lower()} order for {self.instrument_name}{price_desc}"

    def get_calculation_preview(self) -> Dict[str, Any]:
        """
        Get a preview of the calculation without executing.

        Returns:
            Dictionary with reference_price, percent, calculated_price, reference_type
        """
        preview = {
            "reference_type": self.reference_value,
            "percent": self.percent,
            "reference_price": None,
            "calculated_price": self.target_price
        }

        # If price already set, return it
        if self.target_price is not None:
            return preview

        # Try to calculate reference price
        if self.reference_value:
            from ba2_common.core.types import ReferenceValue, OrderRecommendation

            try:
                if self.reference_value == ReferenceValue.ORDER_OPEN_PRICE.value:
                    if self.existing_order:
                        preview["reference_price"] = self.existing_order.limit_price
                elif self.reference_value == ReferenceValue.CURRENT_PRICE.value:
                    preview["reference_price"] = self.get_current_price()
                elif self.reference_value == ReferenceValue.EXPERT_TARGET_PRICE.value:
                    expert_rec = self.expert_recommendation
                    if not expert_rec and self.existing_order and self.existing_order.expert_recommendation_id:
                        from ba2_common.core.db import get_instance
                        from ba2_common.core.models import ExpertRecommendation
                        expert_rec = get_instance(ExpertRecommendation, self.existing_order.expert_recommendation_id)

                    if expert_rec and hasattr(expert_rec, 'price_at_date') and hasattr(expert_rec, 'expected_profit_percent'):
                        base_price = expert_rec.price_at_date
                        expected_profit = expert_rec.expected_profit_percent

                        if expert_rec.recommended_action in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                            preview["reference_price"] = base_price * (1 + expected_profit / 100)
                        elif expert_rec.recommended_action in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                            preview["reference_price"] = base_price * (1 - expected_profit / 100)

                # Calculate final price
                if preview["reference_price"] and self.percent is not None:
                    is_long = (self.order_recommendation in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT))
                    if not is_long and self.existing_order:
                        order_side = str(self.existing_order.side.value if hasattr(self.existing_order.side, 'value') else self.existing_order.side).upper()
                        is_long = (order_side == "BUY")

                    if is_long:
                        preview["calculated_price"] = preview["reference_price"] * (1 + self.percent / 100)
                    else:
                        preview["calculated_price"] = preview["reference_price"] * (1 - self.percent / 100)

            except Exception as e:
                absorb_if_benign(e, InstanceNotFound)
                logger.debug(f"Error calculating {self._label} preview: {e}")

        return preview


class AdjustTakeProfitAction(_AdjustPriceLevelAction):
    """Adjust take profit level for an existing order."""

    _action_type = ExpertActionType.ADJUST_TAKE_PROFIT.value
    _label = "TP"
    _long_label = "Take profit"
    _price_key_prefix = "tp"
    _regime_scale_setting = "regime_tp_scale"
    _result_price_key = "new_tp_price"

    def __init__(self, instrument_name: str, account: AccountInterface,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 expert_recommendation: Optional[ExpertRecommendation] = None,
                 take_profit_price: Optional[float] = None,
                 reference_value: Optional[str] = None, percent: Optional[float] = None):
        super().__init__(instrument_name, account, order_recommendation, existing_order,
                         expert_recommendation, target_price=take_profit_price,
                         reference_value=reference_value, percent=percent)
        self.take_profit_price = self.target_price  # backward-compat alias

    def _call_broker(self, transaction) -> bool:
        return self.account.adjust_tp(transaction, self.target_price, source="ruleset")

    def _post_broker_hook(self, transaction) -> None:
        # Store current target price in transaction meta_data for TradeConditions comparison
        if not transaction.meta_data:
            transaction.meta_data = {}
        if "TradeConditionsData" not in transaction.meta_data:
            transaction.meta_data["TradeConditionsData"] = {}
        transaction.meta_data["TradeConditionsData"]["current_target_price"] = round(self.target_price, 2)
        from ba2_common.core.db import update_instance
        update_instance(transaction)
        logger.info(f"Stored current_target_price=${self.target_price:.2f} in transaction {transaction.id} metadata for TradeConditions")

    def _resolve_min_take_profit_pct(self) -> float:
        """min_take_profit_percent snapshotted on the triggering ExpertRecommendation at
        signal time (mirrors expected_profit_percent's plumbing) -- defaults to 2.0 for rows
        created before this field existed, or when no recommendation is linked. Snapshotted
        rather than read live off an expert instance because TradeActions.py is shared by both
        the live engine and the backtest engine, and only a live expert-instance lookup would
        work in the live path."""
        rec = self.expert_recommendation
        if rec is not None:
            return float(getattr(rec, "min_take_profit_percent", None) or 2.0)
        if self.existing_order and self.existing_order.expert_recommendation_id:
            return resolve_min_take_profit_pct(self.existing_order.expert_recommendation_id)
        return 2.0

    def _enforce_minimum_distance(self) -> None:
        """Floor the TP so it's never closer than min_take_profit_pct%% to the REAL entry FILL
        price (order.limit_price -> order.open_price -> current-price fallback -- the SAME
        chain ORDER_OPEN_PRICE uses) regardless of which reference_value computed
        self.target_price. This is what actually catches an EXPERT_TARGET_PRICE-anchored TP
        that resolves too tight after real slippage on entry: that reference is computed from
        the recommendation's price_at_date, a stale signal-time price that never re-syncs to
        the real fill, so without this floor a slipped entry can silently erode the TP margin
        below the configured minimum.

        NOTE: for a fresh MARKET-order entry this runs in Phase 2, before the order is even
        submitted to the broker, so `limit_price`/`open_price` are both still None and this
        falls back to get_current_price() -- a live quote, not the actual fill. TradeManager
        re-checks this same floor (via compute_tp_floor_price) once the real fill is known,
        in _check_all_waiting_trigger_orders."""
        if not self.existing_order or self.target_price is None:
            return
        entry_price = self.existing_order.limit_price
        if entry_price is None:
            entry_price = self.existing_order.open_price
        if entry_price is None:
            entry_price = self.get_current_price()
        if not entry_price:
            return
        min_pct = self._resolve_min_take_profit_pct()
        side_str = str(self.existing_order.side.value if hasattr(self.existing_order.side, "value")
                       else self.existing_order.side).upper()
        is_long = side_str == "BUY"
        enforced_tp = compute_tp_floor_price(self.target_price, entry_price, min_pct, is_long)
        if enforced_tp is not None:
            actual_pct = (((self.target_price - entry_price) / entry_price) * 100 if is_long
                          else ((entry_price - self.target_price) / entry_price) * 100)
            logger.warning(
                f"TP enforcement: distance from entry {actual_pct:.2f}% below minimum "
                f"{min_pct}%. Adjusting TP from ${self.target_price:.2f} to "
                f"${enforced_tp:.2f} (entry: ${entry_price:.2f})"
            )
            self.target_price = enforced_tp
            self.take_profit_price = self.target_price

    def compute_price(self, order: "TradingOrder") -> Optional[float]:
        """Calculate the take-profit price, enforcing the same real-entry-relative minimum
        distance as _enforce_minimum_distance (see its docstring) -- used by the backtest
        engine's entry_action seeding, which calls compute_price directly rather than
        execute()."""
        price = super().compute_price(order)
        if price is None:
            return None

        entry_price = order.limit_price
        if entry_price is None:
            entry_price = order.open_price
        if entry_price is None:
            entry_price = self.get_current_price()
        if not entry_price:
            return price

        min_pct = self._resolve_min_take_profit_pct()
        if self.order_recommendation in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
            is_long = True
        elif self.order_recommendation in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
            is_long = False
        else:
            order_side = str(order.side.value if hasattr(order.side, 'value') else order.side).upper()
            is_long = (order_side == "BUY")

        enforced_price = compute_tp_floor_price(price, entry_price, min_pct, is_long)
        return enforced_price if enforced_price is not None else price


class AdjustStopLossAction(_AdjustPriceLevelAction):
    """Adjust stop loss level for an existing order."""

    _action_type = ExpertActionType.ADJUST_STOP_LOSS.value
    _label = "SL"
    _long_label = "Stop loss"
    _price_key_prefix = "sl"
    _regime_scale_setting = "regime_stop_scale"
    _result_price_key = "new_sl_price"

    def __init__(self, instrument_name: str, account: AccountInterface,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 expert_recommendation: Optional[ExpertRecommendation] = None,
                 stop_loss_price: Optional[float] = None,
                 reference_value: Optional[str] = None, percent: Optional[float] = None):
        super().__init__(instrument_name, account, order_recommendation, existing_order,
                         expert_recommendation, target_price=stop_loss_price,
                         reference_value=reference_value, percent=percent)
        self.stop_loss_price = self.target_price  # backward-compat alias

    def _call_broker(self, transaction) -> bool:
        # RATCHET-ONLY: a ruleset-driven stop-loss may only TIGHTEN, never loosen. Without this,
        # an always-true SL rule (e.g. condition `has_position`) re-fires every management run and
        # (a) REPLACES the risk manager's safeguard stop with a looser one — the position was
        # SIZED off the safeguard distance, so the realized loss at stop exceeds risk_per_trade_pct
        # — and (b) UN-TRAILS profit-lock tiers: when price falls back under a tier threshold the
        # tier rule stops firing but the base SL rule still does, dropping the stop back below the
        # locked level. Long: never move the stop DOWN once set; short: never UP. Scope is the
        # ruleset path only — manual UI edits and the SmartRM call account.adjust_sl directly with
        # their own source and stay free to loosen deliberately.
        existing = getattr(transaction, "stop_loss", None)
        if existing and existing > 0 and self.target_price:
            side = getattr(transaction, "side", None) or (
                self.existing_order.side if self.existing_order else None)
            side_str = str(side.value if hasattr(side, "value") else side or "").upper()
            is_long = side_str == "BUY"
            loosens = (self.target_price < existing) if is_long else (self.target_price > existing)
            if loosens:
                logger.info(
                    f"SL ratchet: keeping existing stop ${existing:.2f} for transaction "
                    f"{transaction.id} — ruleset asked for ${self.target_price:.2f}, which would "
                    f"LOOSEN the {'long' if is_long else 'short'} stop"
                )
                self.target_price = existing  # result/data reflect the kept (tighter) stop
                self.stop_loss_price = existing
                return True  # no-op success: the tighter stop stands
        return self.account.adjust_sl(transaction, self.target_price, source="ruleset")

    def _post_broker_hook(self, transaction) -> None:
        pass  # SL does not store metadata

    def _enforce_minimum_distance(self) -> None:
        """Enforce a minimum SL distance from CURRENT price, not open/entry price.

        Distance-from-current is direction-aware by construction: a proposed stop that's
        already comfortably far below current price (long) needs no adjustment, whether it's
        a fresh loss-limiting floor set at entry (current ~= open there) or a profit-locking
        tier/trailing stop that's since drifted far below a price that rallied hard. The
        floor only kicks in when the proposed stop sits too close to -- or on the wrong side
        of -- CURRENT price, which is the actual premature-stopout/whipsaw risk this guard
        exists to prevent. (The previous open-price-relative version couldn't distinguish
        "stop too tight" from "stop is a legitimate profit-lock above entry": a trailing stop
        is defined as current_price * (1 - pct/100), i.e. its distance FROM OPEN keeps
        shrinking as price rallies even though its distance from CURRENT price never changes
        -- so it kept getting clobbered back down to a guaranteed loss the moment it moved
        above entry.)"""
        if self.existing_order and self.target_price:
            current_price = self.get_current_price()
            if not current_price:
                return
            from ba2_common.config import get_min_tp_sl_percent
            min_tp_percent = get_min_tp_sl_percent()
            is_long = (self.existing_order.side.upper() == "BUY")

            if is_long:
                actual_percent = ((current_price - self.target_price) / current_price) * 100
                if actual_percent < min_tp_percent:
                    enforced_sl = current_price * (1 - min_tp_percent / 100)
                    logger.warning(
                        f"SL enforcement: distance from current price {actual_percent:.2f}% below minimum {min_tp_percent}%. "
                        f"Adjusting SL from ${self.target_price:.2f} to ${enforced_sl:.2f} (current: ${current_price:.2f})"
                    )
                    self.target_price = enforced_sl
                    self.stop_loss_price = self.target_price
            else:
                actual_percent = ((self.target_price - current_price) / current_price) * 100
                if actual_percent < min_tp_percent:
                    enforced_sl = current_price * (1 + min_tp_percent / 100)
                    logger.warning(
                        f"SL enforcement: distance from current price {actual_percent:.2f}% below minimum {min_tp_percent}%. "
                        f"Adjusting SL from ${self.target_price:.2f} to ${enforced_sl:.2f} (current: ${current_price:.2f})"
                    )
                    self.target_price = enforced_sl
                    self.stop_loss_price = self.target_price

    def compute_price(self, order: "TradingOrder") -> Optional[float]:
        """Calculate the stop loss price, enforcing minimum distance from CURRENT price.

        Same current-price-relative guard as _enforce_minimum_distance above (see its
        docstring for why open-price-relative was wrong for profit-locking stops)."""
        price = super().compute_price(order)

        # Enforce minimum SL distance from current price
        if price is not None:
            current_price = self.get_current_price()
            if current_price:
                from ba2_common.config import get_min_tp_sl_percent
                min_pct = get_min_tp_sl_percent()

                if self.order_recommendation in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                    is_long = True
                elif self.order_recommendation in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                    is_long = False
                else:
                    order_side = str(order.side.value if hasattr(order.side, 'value') else order.side).upper()
                    is_long = (order_side == "BUY")

                if is_long:
                    actual_pct = ((current_price - price) / current_price) * 100
                    if actual_pct < min_pct:
                        price = current_price * (1 - min_pct / 100)
                else:
                    actual_pct = ((price - current_price) / current_price) * 100
                    if actual_pct < min_pct:
                        price = current_price * (1 + min_pct / 100)

        return price


class IncreaseInstrumentShareAction(TradeAction):
    """Increase position size for an instrument to reach target allocation percentage."""
    
    def __init__(self, instrument_name: str, account: AccountInterface, 
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 expert_recommendation: Optional[ExpertRecommendation] = None,
                 target_percent: Optional[float] = None):
        """
        Initialize increase instrument share action.
        
        Args:
            instrument_name: Instrument name
            account: Account interface
            order_recommendation: Trade recommendation
            existing_order: Existing order (optional)
            expert_recommendation: Expert recommendation for linking
            target_percent: Target percentage of virtual equity (e.g., 15.0 for 15%)
        """
        super().__init__(instrument_name, account, order_recommendation, existing_order, expert_recommendation)
        self.target_percent = target_percent
    
    def execute(self) -> "TradeActionResult":
        """
        Increase position to reach target percentage of virtual equity.
        
        Returns:
            TradeActionResult object containing execution results
        """
        try:
            if self.target_percent is None or self.target_percent <= 0:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Invalid target_percent provided",
                    data={}
                )
            
            # Get expert instance and virtual equity
            expert_instance_id = self.expert_recommendation.instance_id if self.expert_recommendation else None
            if not expert_instance_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="No expert instance ID available",
                    data={}
                )
            
            from ba2_common.core.instance_resolver import get_instance_resolver
            expert = get_instance_resolver().get_expert_instance(expert_instance_id)
            if not expert:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message=f"Expert instance {expert_instance_id} not found",
                    data={}
                )
            
            # Get total virtual equity (allocated capital, not just free cash)
            virtual_equity = expert.get_virtual_balance()
            if virtual_equity is None or virtual_equity <= 0:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Cannot get virtual equity for expert",
                    data={}
                )
            
            # Get max allowed per instrument
            max_percent_per_instrument = expert.settings.get('max_virtual_equity_per_instrument_percent', 10.0)
            if self.target_percent > max_percent_per_instrument:
                logger.warning(f"Target percent {self.target_percent}% exceeds max allowed {max_percent_per_instrument}%. Using max.")
                self.target_percent = max_percent_per_instrument
            
            # Calculate target position value
            target_value = virtual_equity * (self.target_percent / 100.0)

            # Get expert's own position (not broker total which includes other experts)
            current_position_qty = self.get_expert_position()
            if current_position_qty is None:
                current_position_qty = 0.0

            current_price = self.get_current_price()
            if current_price is None:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message=f"Cannot get current price for {self.instrument_name}",
                    data={}
                )

            current_value = abs(current_position_qty) * current_price

            # Calculate additional value needed
            additional_value = target_value - current_value

            if additional_value <= 0:
                logger.info(f"Current position value ${current_value:.2f} already at or above target ${target_value:.2f}")
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message=f"Position already at target (current: {(current_value/virtual_equity*100):.1f}%, target: {self.target_percent}%)",
                    data={"current_value": current_value, "target_value": target_value}
                )

            # Check available balance. get_account_info() is a pydantic TradeAccount on
            # Alpaca (no .get()), a dict on IBKR/TastyTrade and None on auth failure, so
            # read it through the broker-agnostic snapshot seam instead. buying_power is
            # None when the broker did not publish one -- refuse to size rather than
            # substituting a number (platform rule: no fallback values for balances).
            snapshot = self.account.get_account_snapshot()
            account_balance = snapshot.buying_power
            if account_balance is None:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message=f"Buying power unavailable for account {self.account.id}",
                    data={},
                )
            clamped_to_buying_power = additional_value > account_balance
            if clamped_to_buying_power:
                logger.warning(f"Additional value ${additional_value:.2f} exceeds available balance ${account_balance:.2f}")
                additional_value = account_balance

            # FLOOR, never round, and never `max(1.0, ...)`. The clamp above is the only
            # thing standing between this action and an order the account cannot pay for,
            # and `max(1.0, round(x))` defeated it outright: 0 buying power still emitted
            # a BUY 1, and $150 of buying power at $100 a share emitted a BUY 2. The
            # downstream catch is SmartRiskManagerQueue's own broken pydantic .get(), so
            # nothing reliably rejects an oversized order on Alpaca today -- the number
            # has to be right here.
            additional_qty = float(math.floor(additional_value / current_price))

            if additional_qty <= 0:
                # Emitting a doomed order is worse than declining: it fails at the broker,
                # leaves an ERROR row and tells the user nothing. Name the binding
                # constraint instead.
                message = (
                    f"Insufficient buying power for one share of {self.instrument_name} "
                    f"(${account_balance:.2f} available, ${current_price:.2f} per share)"
                    if clamped_to_buying_power else
                    f"Increase of ${additional_value:.2f} is less than one share of "
                    f"{self.instrument_name} at ${current_price:.2f}"
                )
                logger.info(f"Not increasing {self.instrument_name}: {message}")
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message=message,
                    data={"current_value": current_value, "target_value": target_value,
                          "buying_power": account_balance},
                )

            logger.info(f"Increasing {self.instrument_name}: expert_qty={current_position_qty}, additional={additional_qty}, "
                       f"target_value=${target_value:.2f} ({self.target_percent}% of ${virtual_equity:.2f})")

            # Determine side based on current position or recommendation
            if current_position_qty >= 0:
                side = "BUY"
            else:
                side = "SELL"  # Short position - sell more
            
            # Create market order. create_order_record() ALREADY persists the row and
            # returns its integer id (see SellAction, which uses it correctly) -- the old
            # code fed that int back into add_instance(), raising UnmappedInstanceError
            # ("Class 'builtins.int' is not mapped") on every single invocation.
            order_id = self.create_order_record(
                side=side,
                quantity=additional_qty,
                order_type="market"
            )

            if not order_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Failed to create order record",
                    data={}
                )

            logger.info(f"Created increase share order {order_id}: {side} {additional_qty} {self.instrument_name}")
            
            return self.create_and_save_action_result(
                action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                success=True,
                message=f"Created order to increase {self.instrument_name} to {self.target_percent}% of portfolio",
                data={
                    "order_id": order_id,
                    "quantity": additional_qty,
                    "side": side,
                    "current_percent": (current_value / virtual_equity * 100),
                    "target_percent": self.target_percent
                }
            )
            
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error executing increase instrument share action for {self.instrument_name}: {e}", exc_info=True)
            return self.create_and_save_action_result(
                action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                success=False,
                message=f"Error: {str(e)}",
                data={}
            )
    
    def get_description(self) -> str:
        """Get description of increase instrument share action."""
        return f"Increase {self.instrument_name} position to {self.target_percent}% of virtual equity"


class DecreaseInstrumentShareAction(TradeAction):
    """Decrease position size for an instrument to reach target allocation percentage."""
    
    def __init__(self, instrument_name: str, account: AccountInterface, 
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 expert_recommendation: Optional[ExpertRecommendation] = None,
                 target_percent: Optional[float] = None):
        """
        Initialize decrease instrument share action.
        
        Args:
            instrument_name: Instrument name
            account: Account interface
            order_recommendation: Trade recommendation
            existing_order: Existing order (optional)
            expert_recommendation: Expert recommendation for linking
            target_percent: Target percentage of virtual equity (e.g., 5.0 for 5%)
        """
        super().__init__(instrument_name, account, order_recommendation, existing_order, expert_recommendation)
        self.target_percent = target_percent
    
    def execute(self) -> "TradeActionResult":
        """
        Decrease position to reach target percentage of virtual equity.
        Maintains minimum of 1 share if not fully closing.
        
        Returns:
            TradeActionResult object containing execution results
        """
        try:
            if self.target_percent is None or self.target_percent < 0:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Invalid target_percent provided",
                    data={}
                )
            
            # Get expert instance and virtual equity
            expert_instance_id = self.expert_recommendation.instance_id if self.expert_recommendation else None
            if not expert_instance_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="No expert instance ID available",
                    data={}
                )
            
            from ba2_common.core.instance_resolver import get_instance_resolver
            expert = get_instance_resolver().get_expert_instance(expert_instance_id)
            if not expert:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message=f"Expert instance {expert_instance_id} not found",
                    data={}
                )
            
            # Get total virtual equity (allocated capital, not just free cash)
            virtual_equity = expert.get_virtual_balance()
            if virtual_equity is None or virtual_equity <= 0:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Cannot get virtual equity for expert",
                    data={}
                )
            
            # Calculate target position value
            target_value = virtual_equity * (self.target_percent / 100.0)

            # Get expert's own position (not broker total which includes other experts)
            current_position_qty = self.get_expert_position()
            if current_position_qty is None or current_position_qty == 0:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="No position to decrease",
                    data={}
                )

            current_price = self.get_current_price()
            if current_price is None:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message=f"Cannot get current price for {self.instrument_name}",
                    data={}
                )

            current_value = abs(current_position_qty) * current_price

            # Calculate reduction needed
            reduction_value = current_value - target_value

            if reduction_value <= 0:
                logger.info(f"Current position value ${current_value:.2f} already at or below target ${target_value:.2f}")
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message=f"Position already at target (current: {(current_value/virtual_equity*100):.1f}%, target: {self.target_percent}%)",
                    data={"current_value": current_value, "target_value": target_value}
                )

            # Calculate quantity to sell. FLOOR, and cap at what is actually held:
            # `round()` rounds UP, and the "keep at least 1 share" clamp below is gated
            # on `target_percent > 0`, so a 0% target on a 2.6-share holding produced a
            # SELL 3 -- an oversell straight into a 0.4-share SHORT. get_expert_position()
            # returns this EXPERT's slice while the broker nets every expert into one
            # position, so that oversell can even SUCCEED, by taking shares off another
            # expert. Flooring under-trims instead, which is recoverable.
            reduction_qty = min(math.floor(reduction_value / current_price),
                                abs(current_position_qty))

            if reduction_qty <= 0:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message=(f"Reduction of ${reduction_value:.2f} is less than one share "
                             f"of {self.instrument_name} at ${current_price:.2f}"),
                    data={"current_value": current_value, "target_value": target_value},
                )

            # Ensure we keep at least 1 share if not closing completely
            remaining_qty = abs(current_position_qty) - reduction_qty
            if self.target_percent > 0 and remaining_qty < 1:
                # Adjust to keep minimum 1 share
                reduction_qty = abs(current_position_qty) - 1
                if reduction_qty < 1:
                    return self.create_and_save_action_result(
                        action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                        success=False,
                        message="Cannot reduce position while maintaining minimum 1 share",
                        data={}
                    )

            logger.info(f"Decreasing {self.instrument_name}: expert_qty={current_position_qty}, reduction={reduction_qty}, "
                       f"target_value=${target_value:.2f} ({self.target_percent}% of ${virtual_equity:.2f})")

            # Determine side (opposite of current position)
            if current_position_qty > 0:
                side = "SELL"  # Close long position
            else:
                side = "BUY"   # Cover short position
            
            # Create market order. create_order_record() ALREADY persists the row and
            # returns its integer id (see SellAction, which uses it correctly) -- the old
            # code fed that int back into add_instance(), raising UnmappedInstanceError
            # ("Class 'builtins.int' is not mapped") on every single invocation. Same
            # defect as IncreaseInstrumentShareAction, but this path needs no
            # buying-power read to reach it, so it was broken on EVERY broker.
            order_id = self.create_order_record(
                side=side,
                quantity=reduction_qty,
                order_type="market"
            )

            if not order_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Failed to create order record",
                    data={}
                )

            logger.info(f"Created decrease share order {order_id}: {side} {reduction_qty} {self.instrument_name}")
            
            return self.create_and_save_action_result(
                action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                success=True,
                message=f"Created order to decrease {self.instrument_name} to {self.target_percent}% of portfolio",
                data={
                    "order_id": order_id,
                    "quantity": reduction_qty,
                    "side": side,
                    "current_percent": (current_value / virtual_equity * 100),
                    "target_percent": self.target_percent,
                    "remaining_qty": remaining_qty
                }
            )
            
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error executing decrease instrument share action for {self.instrument_name}: {e}", exc_info=True)
            return self.create_and_save_action_result(
                action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                success=False,
                message=f"Error: {str(e)}",
                data={}
            )
    
    def get_description(self) -> str:
        """Get description of decrease instrument share action."""
        return f"Decrease {self.instrument_name} position to {self.target_percent}% of virtual equity"


# Factory function to create actions based on action type
class _OptionEntryAction(TradeAction):
    """Shared base for option-entry actions (BuyCall / BullCallSpread / CoveredCall).

    Provides capability guard, chain fetch, contract selection, pct_equity
    sizing, and the submit_to_broker gate.

    TWO SUBCLASS CONTRACTS COEXIST, and that is a migration state, not a design.
    The 2026-08-27 resolve split (phase 2a) moved the seven PREMIUM-SIZED builders
    onto `_resolve()`, which selects contract(s), builds legs, computes the limit
    premium (buy@ask / sell@bid) and returns a `ResolvedStructure` carrying
    everything EXCEPT quantity -- the shared `_size_and_submit()` tail then sizes
    and submits it. The remaining ten (8 reserve-sized, 2 held-shares overlays)
    still implement `_build_and_submit()` and reach the broker themselves; the base
    `_resolve()` bridges them so `execute()` has one entry point for both.

    A refusal from either shape is the same thing: the `self._result(False, ...)`
    dict, NOT a typed `StructureRefusal`. `_result` persists a `TradeActionResult`
    row that the UI reads, so returning a pure value object would silently stop
    writing it.

    Phases 2b and 2c convert the other ten; when the last `_build_and_submit`
    definition is gone, the bridge in the base `_resolve()` should become a raise.
    """

    OPTION_TYPE: OptionRight = OptionRight.CALL

    def __init__(self, instrument_name: str, account: AccountInterface,
                 order_recommendation: OrderRecommendation,
                 existing_order: Optional[TradingOrder] = None,
                 expert_recommendation: Optional[ExpertRecommendation] = None,
                 strike_method: Optional[str] = None,
                 strike_param: Any = None,
                 dte_min: Optional[int] = None,
                 dte_max: Optional[int] = None,
                 sizing: Optional[float] = None,
                 min_open_interest: Optional[int] = None,
                 max_spread_pct: Optional[float] = None,
                 min_volume: Optional[int] = None,
                 wing_width_pct: Optional[float] = None,
                 min_arc: Optional[float] = None,
                 entry_cross: Optional[float] = None,
                 **kwargs):
        super().__init__(instrument_name, account, order_recommendation,
                         existing_order, expert_recommendation)
        self.strike_method = strike_method
        self.strike_param = strike_param
        self.dte_min = dte_min
        self.dte_max = dte_max
        self.sizing = sizing
        self.min_open_interest = min_open_interest
        self.max_spread_pct = max_spread_pct
        # Minimum DAILY TRADED VOLUME for a contract to be selectable. Opt-in (None = off).
        # See option_selector.passes_liquidity: the cache has no open_interest, so volume is
        # the usable liquidity signal, and the fill engine's participation cap means a
        # too-thin contract yields an order that can never fill.
        self.min_volume = min_volume
        self.wing_width_pct = wing_width_pct
        # PREMIUM-RICHNESS floor: the minimum per-contract annualised return on collateral a
        # CREDIT structure must offer to be opened, as a FRACTION (0.15 == 15 %/yr). Opt-in
        # (None = no floor = today's "any positive net credit will do"). Read only by the
        # credit builders, via _refuse_if_arc_below_floor -- a debit structure posts no
        # collateral, so its return ON collateral is undefined rather than zero, and gating
        # it here would refuse every long option ever opened. See core.option_economics.
        self.min_arc = min_arc
        # ENTRY-QUOTE CONCESSION: the fraction of the contract's own MODELLED bid-ask spread
        # this entry gives up when it quotes (0 = the mid, 1 = the far touch). None/0.0 is the
        # pre-F3 quote exactly. Only a simulator that models a spread can answer it, so in
        # live it is inert by construction -- see core.option_entry_quote.
        self.entry_cross = entry_cross

    # --- helpers ----------------------------------------------------------
    def _action_type_value(self) -> str:
        raise NotImplementedError

    def _supports_options(self) -> bool:
        return isinstance(self.account, OptionsAccountInterface)

    def _today(self) -> date:
        """The 'now' date for DTE/expiry windows.

        Live accounts have no simulated clock, so this is the wall-clock ``date.today()``.
        A BACKTEST account exposes its simulated bar date via ``_as_of_date()``; using it
        anchors the chain-fetch expiry window and ``filter_dte`` on the SIMULATED clock
        rather than wall-clock — without it a historical contract is excluded (its expiry
        is years before ``date.today()``), so the option entry never fires AND it would
        leak look-ahead. The accessor is duck-typed (``getattr``) so live behaviour is
        byte-identical (no ``_as_of_date`` -> ``date.today()``)."""
        as_of = getattr(self.account, "_as_of_date", None)
        if callable(as_of):
            try:
                d = as_of()
                if d is not None:
                    return d
            except Exception:  # noqa: BLE001 — never let clock lookup break the action
                pass
        return date.today()

    def _spot(self) -> Optional[float]:
        """Underlying mid price; fall back to default current price."""
        try:
            price = self.account.get_instrument_current_price(self.instrument_name, 'mid')
            if price is not None:
                return price
        except TypeError:
            # Mock/account without price_type support
            pass
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.debug(f"_spot mid lookup failed for {self.instrument_name}: {e}")
        return self.get_current_price()

    def _expiry_window(self, today: date) -> Tuple[date, date]:
        """The [expiry_min, expiry_max] fetch window, or a LOUD config error.

        ``dte_max`` used to default to ``today`` when unset, so a rule with dte_min=30 and no
        dte_max asked the provider for the INVERTED window [today+30, today] — which of
        course returns nothing, and was then reported as "Empty option chain", i.e. as a data
        problem when it is a configuration problem. Same for dte_min > dte_max. A DTE window
        that cannot contain any expiry is never what the user meant, so say so."""
        if self.dte_max is None:
            raise OptionDteWindowError(
                f"Option DTE window for {self.instrument_name} is unusable: dte_max is not "
                f"set (dte_min={self.dte_min}), which asks for the empty/inverted expiry "
                f"window [today+{self.dte_min or 0}, today]. Set dte_max.")
        if self.dte_min is not None and self.dte_min > self.dte_max:
            raise OptionDteWindowError(
                f"Option DTE window for {self.instrument_name} is inverted: "
                f"dte_min={self.dte_min} > dte_max={self.dte_max}; no expiry can fall in it.")
        expiry_min = today + timedelta(days=self.dte_min) if self.dte_min is not None else today
        return expiry_min, today + timedelta(days=self.dte_max)

    def _chain(self, option_type: OptionRight) -> List[OptionContract]:
        today = self._today()
        expiry_min, expiry_max = self._expiry_window(today)
        return self.account.get_option_chain(
            self.instrument_name, expiry_min, expiry_max, option_type)

    def _liq(self, *chains: List[OptionContract]) -> Dict[str, Any]:
        """The liquidity gates EVERY leg of this structure must be selected under.

        Two jobs. (1) It validates the configured gates against the fetched chain(s) once —
        see option_selector.OptionLiquidityDataUnavailable for why a gate nobody can answer
        must be an error rather than a 100% rejection. Validating HERE (on the whole chain,
        once per action) and not inside the selectors is deliberate: several structures call
        a selector with an already-narrowed candidate list (the straddle's same-strike puts,
        the short strangle's expiry re-pin), where a lone missing value says something about
        that contract, not about the data source. (2) It is the SINGLE place the three gate
        values are spelled out, so a leg physically cannot be selected under different gates
        than its siblings — which is how ``min_volume`` came to be applied to the protective
        wings of the iron condor / jade lizard / butterfly / ratio spread but NOT to their
        risk-bearing short legs.

        EACH CHAIN IS CHECKED ON ITS OWN (2026-08-23). This used to flatten every argument
        into one ``universe`` list, which quietly undid job (1) for two-sided structures: the
        call chain and the put chain are separate fetches, so a source that answers one and
        not the other left a single publishing call vouching for a put chain that published
        nothing — the availability probe passed, every put was then fail-closed out, and the
        result read "No liquid ATM put", i.e. a thin market. Which is precisely the silent
        100% rejection the probe was added to abolish. The check has to be meaningful for the
        chain each leg is actually selected from, so it runs once per chain."""
        gates = {"min_open_interest": self.min_open_interest,
                 "max_spread_pct": self.max_spread_pct,
                 "min_volume": self.min_volume}
        # Which source is being asked, so a field it has never published (a structural gap,
        # worth an ERROR and a config change) is not confused with one missing from today's
        # fetch (transient, WARNING, nothing to change). See OptionLiquidityDataMissingToday.
        source = type(self.account).__name__
        for chain in chains:
            check_liquidity_data_available(chain, underlying=self.instrument_name,
                                           source=source, **gates)
        return gates

    def _virtual_equity(self) -> Optional[float]:
        """balance * virtual_equity_pct/100 (defaults to balance when unknown)."""
        balance = self.account.get_balance()
        if balance is None:
            return None
        pct = 100.0
        instance_id = self.expert_recommendation.instance_id if self.expert_recommendation else None
        if instance_id:
            try:
                from ba2_common.core.models import ExpertInstance
                ei = get_instance(ExpertInstance, instance_id)
                if ei is not None and ei.virtual_equity_pct is not None:
                    pct = ei.virtual_equity_pct
            except Exception as e:
                absorb_if_benign(e, InstanceNotFound)
                logger.debug(f"_virtual_equity: could not load ExpertInstance {instance_id}: {e}")
        return balance * (pct / 100.0)

    def _max_equity_per_instrument_cap(self, equity: float) -> Optional[float]:
        """Dollar ceiling from max_virtual_equity_per_instrument_percent -- the SAME
        per-instrument cap the classic equity RM path enforces (TradeRiskManagement.py's
        EARLY SKIP / available_for_instrument), reused here so a single option position
        (one lumpy/expensive contract, or a whole spread) can't consume more of the account
        than that already-GA-optimized cap allows -- independent of option_sizing's own
        (usually smaller, structure-specific) budget, which otherwise has no ceiling at all.

        Best-effort: returns None (no cap applied) if the expert instance or its setting
        can't be resolved. This is a SUPPLEMENTARY safety net layered on top of
        option_sizing, not a hard requirement to trade -- a resolution hiccup must not
        block an otherwise-valid entry option_sizing already approved."""
        instance_id = self.expert_recommendation.instance_id if self.expert_recommendation else None
        if not instance_id:
            return None
        try:
            from ba2_common.core.instance_resolver import get_instance_resolver
            expert = get_instance_resolver().get_expert_instance(instance_id)
            if not expert:
                return None
            pct = expert.settings.get('max_virtual_equity_per_instrument_percent')
            if pct is None:
                return None
            return equity * (float(pct) / 100.0)
        except Exception as e:
            # DELIBERATELY broad: the resolver is INJECTED, so it can fail in ways this module
            # cannot enumerate, and this cap is an optional refinement -- per the docstring it
            # must never block an entry option_sizing already approved. Naming Exception states
            # that choice in code instead of leaving it implicit.
            absorb_if_benign(e, Exception)
            logger.debug(f"_max_equity_per_instrument_cap: could not resolve expert {instance_id}: {e}")
            return None

    def _size_by_cost(self, cost_per_contract: Optional[float],
                      sizing_pct: Optional[float]) -> int:
        """floor(virtual_equity * sizing% / cost_per_contract), capped as before.

        The single sizer ``_size`` and ``_size_by_reserve`` both reduce to. They remain on the
        class (tests and the classic-RM path reference them) and now delegate here, so there is
        one definition of the cap interaction rather than two copies that can drift.
        """
        if not cost_per_contract or cost_per_contract <= 0:
            return 0
        if not sizing_pct or sizing_pct <= 0:
            return 0
        equity = self._virtual_equity()
        if equity is None or equity <= 0:
            return 0
        budget = equity * (sizing_pct / 100.0)
        cap = self._max_equity_per_instrument_cap(equity)
        if cap is not None:
            budget = min(budget, cap)
        return int(math.floor(budget / cost_per_contract))

    def _size(self, premium: float, sizing_pct: Optional[float]) -> int:
        """floor(virtual_equity * sizing% / (premium * 100)); 0 if not sizeable.

        The sizing budget is additionally capped by max_virtual_equity_per_instrument_percent
        (see _max_equity_per_instrument_cap) -- whichever of the two budgets is tighter wins.

        Now a thin adapter over ``_size_by_cost``: a premium is a per-SHARE price, so one
        contract costs ``premium * 100``.

        NO PRODUCTION CALLER REMAINS as of phase 2a -- the seven premium-sized builders go
        through ``_size_and_submit`` now, and ``grep -rn "\._size\b"`` finds only tests. An
        earlier version of this line claimed "tests and the equity-side RM reference it", which
        was half wrong: the equity RM has its own sizer and never called this. It is kept for
        ``test_option_entry_sizing_cap.py``, which asserts the premium-side per-instrument cap
        through this signature. Delete it only together with re-pointing that test at
        ``_size_by_cost(premium * 100.0, pct)``."""
        if premium is None or premium <= 0:
            return 0
        return self._size_by_cost(premium * 100.0, sizing_pct)

    def _size_by_reserve(self, reserve_per_contract: float,
                         sizing_pct: Optional[float]) -> int:
        """floor(virtual_equity * sizing% / reserve_per_contract). For credit/naked
        structures where net premium is negative (can't size off premium). Same
        max_virtual_equity_per_instrument_percent cap as _size() applies here too.

        Adapter over ``_size_by_cost``: the collateral IS the per-contract cost."""
        return self._size_by_cost(reserve_per_contract, sizing_pct)

    def _held_equity_shares(self) -> Optional[float]:
        """Net filled equity shares across this expert's OPENED transactions for the symbol.

        TRI-STATE. A float (INCLUDING ``0.0``) is a MEASUREMENT; ``None`` means the count
        is UNMEASURABLE and the caller must refuse. This is what SIZES a covered call and a
        protective put (``floor(held / 100)``), so a wrong number here is contracts, and on
        the short side it is NAKED contracts.

        A CANCEL DOES NOT UN-TRADE A SHARE. The loop used to skip anything not in
        ``get_executed_statuses()``, and ``CANCELED`` is not in it — but a cancel-and-replace
        that races a fill leaves the SELL ``CANCELED`` with ``filled_qty=100``, and those
        100 shares are genuinely gone. ``reconcile_canceled_partial_fill`` repairs
        ``Transaction.quantity`` and writes no compensating order row, so this loop counted
        the full 200 and wrote 2 contracts against 100 held. The codebase already applies
        exactly this compensation in ``ReadOnlyAccountInterface``'s transaction
        recalculation (``if order.status in executed_statuses or filled_qty > 0``); the
        sizer was the one place that did not. It applies on BOTH sides: a cancelled BUY that
        filled 100 really does hold 100.

        AN UNKNOWN FILL IS NOT A ZERO FILL. ``if not qty: continue`` folded ``None`` — the
        broker said it executed and never said how much — into ``0.0``, a measurement. On a
        SELL that reads as "sold nothing", so shares that may already be gone keep counting
        as cover: an asymmetric fail-OPEN with a concrete live write path. ``Transaction.
        get_current_open_qty`` refuses to make that collapse and logs it; this does the
        same, and because a share count cannot carry a tri-state in a float, the refusal is
        the ``None`` return.

        Note ``0.0`` for a missing expert instance and for no open transactions stays a
        MEASUREMENT, not an unknown: there is genuinely nothing held under that scope.
        """
        instance_id = self.expert_recommendation.instance_id if self.expert_recommendation else None
        if not instance_id:
            return 0.0
        # transactions_where/orders_where are the dual-path equivalents of the raw
        # select() calls this replaced (review 2026-07-18, M2): those silently found
        # nothing when the backtest in-mem store is active (Transaction/TradingOrder are
        # in-mem models, see trade_store.IN_MEM_MODELS).
        from ba2_common.core.trade_store import transactions_where, orders_where
        total = 0.0
        txns = transactions_where(symbol=self.instrument_name, expert_id=instance_id,
                                  status=TransactionStatus.OPENED)
        txn_ids = [t.id for t in txns]
        if not txn_ids:
            return 0.0
        orders = orders_where(transaction_ids=txn_ids)
        executed = OrderStatus.get_executed_statuses()
        for o in orders:
            if o.asset_class == AssetClass.OPTION:
                continue
            filled = o.filled_qty
            traded = (filled is not None and filled > 0)
            if o.status not in executed and not traded:
                continue
            if filled is None:
                logger.error(
                    f"_held_equity_shares({self.instrument_name}): order {o.id} "
                    f"({o.symbol} {o.side}) is {o.status} but carries NO filled_qty — how "
                    f"many shares traded is UNMEASURABLE, not zero. Reporting the share "
                    f"count as unknown so the covered call / protective put refuses rather "
                    f"than sizing off a number that may already be wrong.")
                return None
            if o.side == OrderDirection.BUY:
                total += abs(float(filled))
            else:
                total -= abs(float(filled))
        return total

    def _consensus_target(self) -> Optional[float]:
        """Resolve a target price for consensus_target strike selection."""
        rec = self.expert_recommendation
        if rec is None:
            return None
        data = rec.data or {}
        fmp = data.get("FMPRating") if isinstance(data, dict) else None
        if isinstance(fmp, dict) and fmp.get("target_consensus") is not None:
            return fmp["target_consensus"]
        price = rec.price_at_date
        epp = rec.expected_profit_percent
        if price is None or epp is None:
            return None
        action = rec.recommended_action
        if action in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
            return price * (1 + epp / 100.0)
        if action in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
            return price * (1 - epp / 100.0)
        return None

    def _result(self, success: bool, message: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.create_and_save_action_result(
            action_type=self._action_type_value(), success=success, message=message, data=data or {})

    def _refuse_if_cannot_take_delivery(self, option_strategy: str, *,
                                        strike: Optional[float],
                                        contracts: Optional[float]
                                        ) -> Optional[Dict[str, Any]]:
        """ENFORCING assignment-capacity gate. Returns a refusal, or None to proceed.

        Only structures carrying a SHORT PUT reach here, and they pass their short put
        LEG (its strike and its total contract count across the whole order, ratios
        included) rather than a dollar figure — the pricing lives in one place,
        ``option_lifecycle.put_assignment_cost``, reached via
        ``check_short_put_assignment_capacity``.

        WHY THIS IS NOT THE BUYING-POWER GATE AGAIN. The reserve pool asks "can I set
        aside what this ONE structure needs?", priced the way a broker prices it —
        Reg-T naked margin (~20% of notional) for a short strangle, the wing width for
        an iron condor. This asks "if EVERY open short put were assigned tonight, could
        we pay for the shares?", which is one number for all of them: the strike. A book
        of short straddles can pass the first gate five times over and still owe more
        delivery than the account holds — measured, five 100-strike puts reserving
        $2,000 each against $45,000 of cash and a $50,000 bill.

        Placed AFTER ``check_option_buying_power`` deliberately: an entry that the
        pre-existing gate already refuses keeps the message it has always had, so no
        decline already on record is re-labelled and the new refusal only ever appears
        where something genuinely new is being caught.
        """
        verdict = self.account.check_short_put_assignment_capacity(
            strike=strike, contracts=contracts)
        if verdict.ok:
            return None
        logger.warning(f"{self._action_type_value()} for {self.instrument_name}: "
                       f"{option_strategy} REFUSED — {verdict.reason}")
        return self._result(
            False, f"{verdict.reason} Refusing {option_strategy} on {self.instrument_name}.",
            {"option_strategy": option_strategy,
             "assignment_held_cost": verdict.held_cost,
             "assignment_candidate_cost": verdict.candidate_cost,
             "assignment_cash": verdict.cash})

    def _refuse_if_arc_below_floor(self, option_strategy: str, *,
                                   net_credit: Optional[float],
                                   expiry: Optional[date],
                                   **reserve_kwargs) -> Optional[Dict[str, Any]]:
        """ENFORCING premium-richness gate. Returns a refusal, or None to proceed.

        THE CRITERION THAT DID NOT EXIST (OPT-C1). Every credit builder admitted its
        structure on ``net_credit > 0`` alone: no minimum credit, no credit-as-a-fraction
        of width, no return floor. Selling far-OTM options for a nickel expires worthless
        roughly 97 % of the time, so on any win-rate- or Sharpe-flavoured fitness a search
        is ACTIVELY REWARDED for doing it. ``option_economics`` prices the alternative --
        annualised return on collateral, PER CONTRACT, so the ratio is invariant to size --
        and this is the one call that makes it bite.

        A VERDICT, NOT A RAISE. This is an operational refusal (the structure on offer is
        not rich enough today), the same category as buying power and assignment capacity,
        so it comes back as a failed ``TradeActionResult`` the caller can record. Raising
        is reserved for caller errors -- five legs, two expiries, an inverted DTE window.

        Placed beside each builder's ``net_credit <= 0`` check, as soon as the collateral
        inputs are known and always BEFORE sizing/submission: an entry the pre-existing
        checks already decline keeps the message it has always had, and this refusal only
        ever appears where something genuinely new is being caught.

        ``reserve_kwargs`` are the sizing inputs that structure's reserve branch prices
        with (``strike`` / ``spread_width`` / ``spot`` / ``option_type``) -- the same ones
        the builder passes to ``option_reserve_required`` two lines later, so collateral is
        never derived twice.

        No floor configured (the default) short-circuits to None: the gate is off, exactly
        as it was, and no ARC is computed. A floor that is configured but UNMEASURABLE
        against refuses -- an unmeasurable entry criterion is not a satisfied one.
        """
        if self.min_arc is None:
            return None
        dte = (expiry - self._today()).days if expiry is not None else None
        arc = annualized_return_on_collateral(
            strategy=option_strategy, net_credit=net_credit, days_to_expiry=dte,
            **reserve_kwargs)
        if admits_credit_structure(arc, self.min_arc):
            return None
        measured = "unmeasurable" if arc is None else f"{arc * 100:.2f}%/yr"
        logger.warning(f"{self._action_type_value()} for {self.instrument_name}: "
                       f"{option_strategy} REFUSED — {ARC_FLOOR_REFUSAL} "
                       f"({measured} vs {self.min_arc})")
        return self._result(
            False,
            f"{option_strategy} on {self.instrument_name}: {ARC_FLOOR_REFUSAL} "
            f"(measured {measured}, floor {self.min_arc}). Refusing the entry.",
            {"option_strategy": option_strategy, "arc": arc, "min_arc": self.min_arc,
             "net_credit": net_credit, "days_to_expiry": dte})

    def _refuse_if_cover_is_short(self, legs: List[OptionLeg], quantity: int,
                                  option_strategy: str) -> Optional[Dict[str, Any]]:
        """ENFORCING share-cover gate. Returns a refusal, or None to proceed.

        THE THIRD RAIL, DELIVERED THE SAME WAY AS THE OTHER TWO. Buying power,
        assignment capacity and share cover all refuse the same entry path with three
        different remedies, and the first two arrive here as a returned verdict that
        becomes a failed ``TradeActionResult``. Cover was decided at the broker seam and
        RAISED, which is the channel the structural validations use (five legs, two
        expiries) — malformed calls, not outcomes. Raised from here it propagates out of
        ``execute()`` and out of ``TradeActionEvaluator``, so every action queued behind
        it on this instrument is skipped with no result row written for any of them, and
        an ordinary condition — a feed outage, a second call on the same lot, shares not
        yet visible on the broker's side — logs a stack trace at ERROR.

        The seam's raise STAYS as the backstop for direct callers
        (``PremiumSeller.rebalance``, ``OptionPortfolioManager``), which have nowhere to
        put a verdict; this is the ask-first path for the caller that does. The verdict's
        ``reason`` is the seam's own sentence verbatim, so both channels say one thing.

        Placed AFTER sizing, like ``_refuse_if_cannot_take_delivery`` is placed after the
        buying-power gate: an entry the pre-existing "held equity below one contract lot"
        check already declines keeps the message it has always had.
        """
        verdict = self.account.check_cover_for_covered_call(legs, quantity, option_strategy)
        if verdict.ok:
            return None
        logger.warning(f"{self._action_type_value()} for {self.instrument_name}: "
                       f"{option_strategy} REFUSED — {verdict.reason}")
        return self._result(
            False, f"{verdict.reason} Refusing {option_strategy} on {self.instrument_name}.",
            {"option_strategy": option_strategy,
             "cover_underlying": verdict.underlying,
             "cover_required": verdict.required,
             "cover_held": verdict.held,
             "cover_pledged": verdict.pledged})

    def _modelled_half_spreads(self, legs: List[OptionLeg]) -> Optional[List[float]]:
        """One modelled half-spread per leg, or None when this account models no spread.

        Duck-typed on purpose (the same idiom as ``_today``'s ``_as_of_date`` lookup): the hook
        exists only on the BACKTEST account, because only a simulator has a MODELLED spread to
        concede. A live account has real quotes and its builders already quote at the real
        touch, so the absence of the hook is what keeps live byte-identical.

        Returns None -- meaning "no concession" -- if ANY leg cannot be priced by the model.
        A partial concession would be measured against a spread the fill engine will charge in
        full on the missing leg, so falling back to the historical quote is the honest answer.
        """
        hook = getattr(self.account, "option_modelled_half_spread", None)
        if not callable(hook):
            return None
        out: List[float] = []
        for leg in legs:
            half = hook(leg.contract_symbol)
            if half is None:
                logger.debug(f"{self._action_type_value()} for {self.instrument_name}: no "
                             f"modelled spread for {leg.contract_symbol}; quoting the entry "
                             f"unchanged")
                return None
            out.append(float(half))
        return out

    def _quote_with_concession(self, legs: List[OptionLeg], limit_price: float) -> float:
        """``limit_price`` after giving up ``self.entry_cross`` of the modelled spread.

        Three ways to get the untouched quote back, all of them today's behaviour: the gene is
        unset or 0.0, the account models no spread (every live account), or a leg's spread is
        unmodellable. See ``core.option_entry_quote`` for the direction rules and the floors.
        """
        fraction = self.entry_cross
        if fraction is None or float(fraction) == ENTRY_CROSS_NEUTRAL:
            return limit_price
        if limit_price is None:
            return limit_price
        halves = self._modelled_half_spreads(legs)
        if halves is None:
            return limit_price
        quoted = entry_limit_with_concession(float(limit_price), legs, halves, fraction)
        if quoted != limit_price:
            logger.debug(
                f"{self._action_type_value()} for {self.instrument_name}: entry_cross="
                f"{float(fraction):.2f} moved the limit {limit_price:+.4f} -> {quoted:+.4f} "
                f"(modelled concession {quote_concession(legs, halves, fraction):.4f}/share)")
        return quoted

    def _submit_option_order(self, legs: List[OptionLeg], quantity: int,
                             limit_price: float, option_strategy: str,
                             option_reserve: Optional[float] = None) -> Dict[str, Any]:
        """Submit (or defer) the assembled option order, honoring submit_to_broker.

        When `option_reserve` is provided (short-premium strategies: CSP / credit
        spread), it is persisted on the parent order's `data["option_reserve"]` so
        `OptionsAccountInterface.reserved_option_buying_power()` can account for it.

        THE ENTRY-QUOTE CONCESSION IS APPLIED HERE (F3), the one choke point all seventeen
        builders reach, so no builder can be forgotten and none needs to know about it.
        ``entry_cross`` = the fraction of each leg's own modelled spread this entry gives up;
        at the default 0.0 (or on any account with no spread model, i.e. every live one) the
        limit is returned untouched and this method is byte-identical to before.

        AFTER SIZING, DELIBERATELY. ``_size_and_submit`` has already divided the budget by the
        UNCONCEDED cost, and ``option_reserve`` was computed from the unconceded credit. Both
        stay that way: the concession is bounded by one modelled half-spread per leg (2.5 % of
        premium at the grid's ``--option-spread-pct 5.0``), while folding it into the sizing
        would turn a quote gene into a size gene and make ``option_sizing``'s own band mean
        something different at each level of it.
        """
        quoted = self._quote_with_concession(legs, limit_price)
        expert_rec_id = self.expert_recommendation.id if self.expert_recommendation else None
        data = {
            "option_strategy": option_strategy,
            "quantity": quantity,
            "limit_price": quoted,
            "legs": [{"contract_symbol": leg.contract_symbol, "side": leg.side.value,
                      "position_intent": leg.position_intent, "strike": leg.strike}
                     for leg in legs],
        }
        # Only when the concession actually moved the quote, so a default run persists the
        # SAME TradeActionResult data dict it always has.
        if quoted != limit_price:
            data["entry_cross"] = float(self.entry_cross)
            data["entry_quote_concession"] = quoted - limit_price   # signed: shows the direction
        limit_price = quoted
        if option_reserve is not None:
            data["option_reserve"] = option_reserve
        if not self.submit_to_broker:
            logger.info(f"_OptionEntryAction: submit disabled for {self.instrument_name} "
                        f"{option_strategy} - recording informational result")
            return self._result(True,
                                 f"{option_strategy} for {self.instrument_name} (manual review, not submitted)",
                                 data)
        order = self.account.submit_option_order(
            legs=legs, quantity=quantity, order_type="limit", limit_price=limit_price,
            option_strategy=option_strategy, expert_recommendation_id=expert_rec_id)
        if order is None:
            return self._result(False, f"Failed to submit {option_strategy} for {self.instrument_name}", data)
        order_id = getattr(order, "id", None)
        data["order_id"] = order_id
        # Persist the short-premium reserve on the order so available BP reflects it.
        if option_reserve is not None and order_id is not None:
            try:
                stored = get_instance(TradingOrder, order_id)
                if stored is not None:
                    stored.data = {**(stored.data or {}), "option_reserve": option_reserve}
                    update_instance(stored)
            except Exception as e:
                absorb_if_benign(e, InstanceNotFound)
                logger.error(f"Failed to persist option_reserve on order {order_id}: {e}", exc_info=True)
        return self._result(True, f"Submitted {option_strategy} for {self.instrument_name}", data)

    def _build_and_submit(self) -> Dict[str, Any]:
        raise NotImplementedError

    def _resolve(self):
        """Select contracts, build legs, price the structure. Return a ``ResolvedStructure``
        — or, for a refusal, the ``self._result(False, ...)`` dict the builder already returns.

        REFUSALS STAY AS ``_result`` DICTS, not ``StructureRefusal``. ``_result`` PERSISTS a
        ``TradeActionResult`` row (``create_and_save_action_result``), and the UI reads those
        rows; returning a pure value object here would silently stop writing them. The typed
        refusal is a Phase 3 concern, on the risk-manager side.

        NO QUANTITY, AND NO ACCOUNT STATE THAT DEPENDS ON ONE. Everything a single action can
        know about one structure belongs here; everything that needs the size belongs in
        ``_size_and_submit``.

        MIGRATION BRIDGE, DELIBERATE. The default delegates to the legacy ``_build_and_submit``
        because the split lands in three phases: 2a converts the 7 premium-sized builders, 2b
        the 8 reserve-sized ones and 2c the 2 share-overlay ones. An unconverted builder still
        ends at ``_submit_option_order`` and so returns a result dict, which ``execute()``
        passes straight back — byte-identical to calling ``_build_and_submit()`` directly, which
        is what makes this refactor behaviour-neutral for the builders it has not reached yet.
        Delete this body (back to ``raise NotImplementedError``) once ``_build_and_submit`` has
        no definitions left.
        """
        return self._build_and_submit()

    def _dte_for(self, expiry) -> int:
        """Days to expiry against the action's clock (simulated in a backtest, wall in live).

        Broken out because NO builder computed this before — it existed only transiently inside
        ``_refuse_if_arc_below_floor``, and only when an ARC floor was configured.
        ``ResolvedStructure`` needs it unconditionally.
        """
        return (expiry - self._today()).days

    def _size_and_submit(self, resolved) -> Dict[str, Any]:
        """Size ``resolved`` and submit it. The former tail of every ``_build_and_submit``.

        BYTE-IDENTICAL ARITHMETIC TO WHAT IT REPLACES. ``_size(premium, pct)`` computed
        ``floor(budget / (premium * 100))`` and ``_size_by_reserve(reserve, pct)`` computed
        ``floor(budget / reserve)``. Both are ``floor(budget / cost_per_contract)``, which is
        why ``ResolvedStructure`` carries that single number instead of the two inputs.
        """
        quantity = self._size_by_cost(resolved.cost_per_contract, self.sizing)
        if quantity < 1:
            # The structure's OWN historical wording, not a uniform one. These strings are
            # persisted to TradeActionResult.message and rendered in the UI as the reason an
            # entry did not fire; rewording five of seven of them would have made "behaviour
            # neutral" false in the one place a user actually looks.
            return self._result(
                False,
                resolved.budget_refusal_message
                or (f"Insufficient budget to size {resolved.option_strategy} for "
                    f"{self.instrument_name}"))
        return self._submit_option_order(
            resolved.legs, quantity, resolved.limit_price, resolved.option_strategy)

    def execute(self) -> "TradeActionResult":
        try:
            if not self._supports_options():
                return self._result(False, f"Account does not support options for {self.instrument_name}")
            resolved = self._resolve()
            if not isinstance(resolved, ResolvedStructure):
                return resolved          # a refusal dict from _result(False, ...)
            return self._size_and_submit(resolved)
        except OptionLiquidityDataMissingToday as e:
            # NOT a misconfiguration: this source HAS published the field before, so today's
            # chain simply came back without it (Alpaca types open_interest Optional). The
            # entry is still refused — a gate is never applied to a chain that cannot answer
            # it — but a transient broker gap must not shout ERROR once per symbol per day,
            # nor tell the user to clear a gate they will want back tomorrow.
            logger.warning(f"{self._action_type_value()} for {self.instrument_name}: {e}")
            return self._result(False, str(e))
        except OptionSelectionConfigError as e:
            # A parameter that can never select anything (a liquidity gate the data source
            # cannot answer, an inverted DTE window). NOT a runtime failure and not a
            # market condition — surface the exact knob instead of "No liquid <structure>",
            # which reads as "the chain is thin" and sends the user hunting the wrong thing.
            logger.error(f"{self._action_type_value()} for {self.instrument_name} is "
                         f"misconfigured: {e}")
            return self._result(False, str(e))
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error executing {self._action_type_value()} for {self.instrument_name}: {e}",
                         exc_info=True)
            return self._result(False, f"Error executing option action: {str(e)}")


class BuyCallAction(_OptionEntryAction):
    """Buy a single long call (debit) selected from the chain."""

    OPTION_TYPE = OptionRight.CALL

    def _action_type_value(self) -> str:
        return ExpertActionType.BUY_CALL.value

    def _resolve(self):
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        contract = select_single(
            chain, method=self.strike_method, strike_param=self.strike_param, spot=spot,
            option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            **liq)
        if contract is None:
            return self._result(False, f"No liquid call contract for {self.instrument_name}")
        if contract.ask is None or contract.ask <= 0:
            return self._result(False, f"No ask price for {contract.symbol}")
        limit_price = contract.ask                          # buy at ASK
        leg = OptionLeg(contract_symbol=contract.symbol, side=OrderDirection.BUY,
                        position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                        strike=contract.strike, expiry=contract.expiry, underlying=contract.underlying)
        return ResolvedStructure(
            request=None, legs=[leg],
            payoff_legs=[PayoffLeg(kind="call", side=OrderDirection.BUY,
                                   premium=contract.ask, strike=contract.strike)],
            limit_price=limit_price, option_strategy="long_call",
            dte=self._dte_for(contract.expiry), reserve_per_contract=0.0,
            cost_per_contract=limit_price * 100.0, sizing_basis="premium",
            budget_refusal_message=(
                f"Insufficient budget to size long_call for {self.instrument_name} "
                f"(premium={limit_price})"),
            reserve_kwargs={})

    def get_description(self) -> str:
        return f"Buy long call on {self.instrument_name}"


class OpenBullCallSpreadAction(_OptionEntryAction):
    """Open a bull call (debit) vertical spread: buy lower strike, sell higher strike."""

    OPTION_TYPE = OptionRight.CALL

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_BULL_CALL_SPREAD.value

    def _resolve(self):
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        long_param, short_param = self._spread_params()
        pair = select_vertical_spread(
            chain, method=self.strike_method, long_param=long_param, short_param=short_param,
            spot=spot, option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            **liq)
        if pair is None:
            return self._result(False, f"No liquid bull call spread for {self.instrument_name}")
        long_c, short_c = pair
        if long_c.ask is None or short_c.bid is None:
            return self._result(False, f"Missing quote for spread legs on {self.instrument_name}")
        net_debit = round(long_c.ask - short_c.bid, 4)      # buy long@ask, sell short@bid
        if net_debit <= 0:
            return self._result(False,
                                f"Non-positive net debit ({net_debit}) for {self.instrument_name} spread")
        long_leg = OptionLeg(contract_symbol=long_c.symbol, side=OrderDirection.BUY,
                             position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                             strike=long_c.strike, expiry=long_c.expiry, underlying=long_c.underlying)
        short_leg = OptionLeg(contract_symbol=short_c.symbol, side=OrderDirection.SELL,
                              position_intent="sell_to_open", option_type=self.OPTION_TYPE,
                              strike=short_c.strike, expiry=short_c.expiry, underlying=short_c.underlying)
        return ResolvedStructure(
            request=None, legs=[long_leg, short_leg],
            payoff_legs=[PayoffLeg(kind="call", side=OrderDirection.BUY,
                                   premium=long_c.ask, strike=long_c.strike),
                         PayoffLeg(kind="call", side=OrderDirection.SELL,
                                   premium=short_c.bid, strike=short_c.strike)],
            limit_price=net_debit, option_strategy="bull_call_spread",
            dte=self._dte_for(long_c.expiry), reserve_per_contract=0.0,
            cost_per_contract=net_debit * 100.0, sizing_basis="premium",
            budget_refusal_message=(
                f"Insufficient budget to size bull_call_spread for {self.instrument_name} "
                f"(net_debit={net_debit})"),
            reserve_kwargs={})

    def _spread_params(self) -> Tuple[Any, Any]:
        """Split strike_param into (long, short) params for the two legs."""
        sp = self.strike_param
        if isinstance(sp, dict):
            return sp.get("long"), sp.get("short")
        if isinstance(sp, (list, tuple)) and len(sp) == 2:
            return sp[0], sp[1]
        # Single value: use the same param for both legs (selector dedups by strike).
        return sp, sp

    def get_description(self) -> str:
        return f"Open bull call spread on {self.instrument_name}"


class BuyPutAction(_OptionEntryAction):
    """Buy a single long put (debit) selected from the chain."""

    OPTION_TYPE = OptionRight.PUT

    def _action_type_value(self) -> str:
        return ExpertActionType.BUY_PUT.value

    def _resolve(self):
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        contract = select_single(
            chain, method=self.strike_method, strike_param=self.strike_param, spot=spot,
            option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            **liq)
        if contract is None:
            return self._result(False, f"No liquid put contract for {self.instrument_name}")
        if contract.ask is None or contract.ask <= 0:
            return self._result(False, f"No ask price for {contract.symbol}")
        limit_price = contract.ask                          # buy at ASK
        leg = OptionLeg(contract_symbol=contract.symbol, side=OrderDirection.BUY,
                        position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                        strike=contract.strike, expiry=contract.expiry, underlying=contract.underlying)
        return ResolvedStructure(
            request=None, legs=[leg],
            payoff_legs=[PayoffLeg(kind="put", side=OrderDirection.BUY,
                                   premium=contract.ask, strike=contract.strike)],
            limit_price=limit_price, option_strategy="long_put",
            dte=self._dte_for(contract.expiry), reserve_per_contract=0.0,
            cost_per_contract=limit_price * 100.0, sizing_basis="premium",
            budget_refusal_message=(
                f"Insufficient budget to size long_put for {self.instrument_name} "
                f"(premium={limit_price})"),
            reserve_kwargs={})

    def get_description(self) -> str:
        return f"Buy long put on {self.instrument_name}"


class OpenBearPutSpreadAction(_OptionEntryAction):
    """Open a bear put (debit) vertical spread: buy higher strike, sell lower strike."""

    OPTION_TYPE = OptionRight.PUT

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_BEAR_PUT_SPREAD.value

    def _resolve(self):
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        long_param, short_param = self._spread_params()
        pair = select_vertical_spread(
            chain, method=self.strike_method, long_param=long_param, short_param=short_param,
            spot=spot, option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            **liq)
        if pair is None:
            return self._result(False, f"No liquid bear put spread for {self.instrument_name}")
        # For a PUT debit spread the selector returns (long, short) with long.strike > short.strike.
        long_c, short_c = pair
        if long_c.ask is None or short_c.bid is None:
            return self._result(False, f"Missing quote for spread legs on {self.instrument_name}")
        net_debit = round(long_c.ask - short_c.bid, 4)      # buy long@ask, sell short@bid
        if net_debit <= 0:
            return self._result(False,
                                f"Non-positive net debit ({net_debit}) for {self.instrument_name} spread")
        long_leg = OptionLeg(contract_symbol=long_c.symbol, side=OrderDirection.BUY,
                             position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                             strike=long_c.strike, expiry=long_c.expiry, underlying=long_c.underlying)
        short_leg = OptionLeg(contract_symbol=short_c.symbol, side=OrderDirection.SELL,
                              position_intent="sell_to_open", option_type=self.OPTION_TYPE,
                              strike=short_c.strike, expiry=short_c.expiry, underlying=short_c.underlying)
        return ResolvedStructure(
            request=None, legs=[long_leg, short_leg],
            payoff_legs=[PayoffLeg(kind="put", side=OrderDirection.BUY,
                                   premium=long_c.ask, strike=long_c.strike),
                         PayoffLeg(kind="put", side=OrderDirection.SELL,
                                   premium=short_c.bid, strike=short_c.strike)],
            limit_price=net_debit, option_strategy="bear_put_spread",
            dte=self._dte_for(long_c.expiry), reserve_per_contract=0.0,
            cost_per_contract=net_debit * 100.0, sizing_basis="premium",
            budget_refusal_message=(
                f"Insufficient budget to size bear_put_spread for {self.instrument_name} "
                f"(net_debit={net_debit})"),
            reserve_kwargs={})

    def _spread_params(self) -> Tuple[Any, Any]:
        """Split strike_param into (long, short) params for the two legs."""
        sp = self.strike_param
        if isinstance(sp, dict):
            return sp.get("long"), sp.get("short")
        if isinstance(sp, (list, tuple)) and len(sp) == 2:
            return sp[0], sp[1]
        # Single value: use the same param for both legs (selector dedups by strike).
        return sp, sp

    def get_description(self) -> str:
        return f"Open bear put spread on {self.instrument_name}"


class SellCoveredCallAction(_OptionEntryAction):
    """Sell a covered call against a held equity long (one contract per 100 shares)."""

    OPTION_TYPE = OptionRight.CALL

    def _action_type_value(self) -> str:
        return ExpertActionType.SELL_COVERED_CALL.value

    def _build_and_submit(self) -> Dict[str, Any]:
        held = self._held_equity_shares()
        if held is None:
            # UNMEASURABLE, not zero (OPT-L4). An executed order with no filled_qty means
            # the broker did not say how many shares moved; sizing a SHORT CALL off a count
            # that may already be wrong is how a naked contract gets written. FIRST, before
            # any chain fetch — nothing downstream can improve an unknown share count.
            return self._result(False,
                                f"Share count for {self.instrument_name} is UNMEASURABLE — an executed "
                                f"equity order carries no filled_qty, so how many shares are held cannot "
                                f"be measured and a covered call written against them could be naked. "
                                f"Repair the order's filled_qty; unknown is not zero.")
        quantity = int(math.floor(held / 100.0)) if held > 0 else 0
        if quantity < 1:
            return self._result(False,
                                f"Held equity below one contract lot for covered call on {self.instrument_name} "
                                f"(shares={held}, 100 required per contract) - size the equity BUY with "
                                f"lot_size=100 or pick a cheaper underlying")
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        contract = select_single(
            chain, method=self.strike_method, strike_param=self.strike_param, spot=spot,
            option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            **liq)
        if contract is None:
            return self._result(False, f"No liquid call contract for covered call on {self.instrument_name}")
        if contract.bid is None or contract.bid <= 0:
            return self._result(False, f"No bid price for {contract.symbol}")
        limit_price = contract.bid                          # sell at BID
        leg = OptionLeg(contract_symbol=contract.symbol, side=OrderDirection.SELL,
                        position_intent="sell_to_open", option_type=self.OPTION_TYPE,
                        strike=contract.strike, expiry=contract.expiry, underlying=contract.underlying)
        # The ACCOUNT-WIDE cover check, which ``_held_equity_shares`` above is not: that
        # sums THIS expert's own filled buys and consults no short-call book, so a second
        # covered call written against the same lot passes it (and shares bought by
        # another expert are invisible to it). The seam refuses the same write either
        # way — asking here is what turns that refusal into a recorded result instead of
        # an exception that skips every action queued behind this one.
        refusal = self._refuse_if_cover_is_short([leg], quantity, "covered_call")
        if refusal is not None:
            return refusal
        return self._submit_option_order([leg], quantity, limit_price, "covered_call")

    def get_description(self) -> str:
        return f"Sell covered call on {self.instrument_name}"


class BuyProtectivePutAction(_OptionEntryAction):
    """Buy a protective put against a held equity long (one contract per 100 shares)."""

    OPTION_TYPE = OptionRight.PUT

    def _action_type_value(self) -> str:
        return ExpertActionType.BUY_PROTECTIVE_PUT.value

    def _build_and_submit(self) -> Dict[str, Any]:
        held = self._held_equity_shares()
        if held is None:
            # Same accessor, same refusal (OPT-L4). Over-buying protection is not the naked
            # risk the covered call carries, but it is still real money spent against a
            # position whose size nobody can state.
            return self._result(False,
                                f"Share count for {self.instrument_name} is UNMEASURABLE — an executed "
                                f"equity order carries no filled_qty, so how many shares need protecting "
                                f"cannot be measured. Repair the order's filled_qty; unknown is not zero.")
        quantity = int(math.floor(held / 100.0)) if held > 0 else 0
        if quantity < 1:
            return self._result(False,
                                f"Held equity below one contract lot for protective put on {self.instrument_name} "
                                f"(shares={held}, 100 required per contract) - size the equity BUY with "
                                f"lot_size=100 or pick a cheaper underlying")
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        contract = select_single(
            chain, method=self.strike_method, strike_param=self.strike_param, spot=spot,
            option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            **liq)
        if contract is None:
            return self._result(False, f"No liquid put contract for protective put on {self.instrument_name}")
        if contract.ask is None or contract.ask <= 0:
            return self._result(False, f"No ask price for {contract.symbol}")
        limit_price = contract.ask                          # buy at ASK
        leg = OptionLeg(contract_symbol=contract.symbol, side=OrderDirection.BUY,
                        position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                        strike=contract.strike, expiry=contract.expiry, underlying=contract.underlying)
        return self._submit_option_order([leg], quantity, limit_price, "protective_put")

    def get_description(self) -> str:
        return f"Buy protective put on {self.instrument_name}"


class SellCashSecuredPutAction(_OptionEntryAction):
    """Sell a cash-secured put (short premium) and reserve strike*100 per contract.

    Income/entry strategy: collect the put premium (sold at BID); the account must
    reserve cash equal to the assignment cost (strike * 100) per contract so the
    position is fully secured. Assignment risk: if the underlying closes below the
    strike at expiry, the shares are put to the account at the strike.
    """

    OPTION_TYPE = OptionRight.PUT

    def _action_type_value(self) -> str:
        return ExpertActionType.SELL_CASH_SECURED_PUT.value

    def _build_and_submit(self) -> Dict[str, Any]:
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        contract = select_single(
            chain, method=self.strike_method, strike_param=self.strike_param, spot=spot,
            option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            **liq)
        if contract is None:
            return self._result(False, f"No liquid put contract for cash-secured put on {self.instrument_name}")
        if contract.bid is None or contract.bid <= 0:
            return self._result(False, f"No bid price for {contract.symbol}")
        if contract.strike is None or contract.strike <= 0:
            return self._result(False, f"No strike for {contract.symbol}")
        # PREMIUM RICHNESS (OPT-C1): a positive bid is not a reason to tie up strike*100.
        refusal = self._refuse_if_arc_below_floor(
            "cash_secured_put", net_credit=contract.bid, expiry=contract.expiry,
            strike=contract.strike)
        if refusal is not None:
            return refusal
        # Sizing: budget by the cash that must be reserved (strike*100), not the premium.
        # Routed through the shared _size_by_reserve() (not inlined) so it also gets capped by
        # max_virtual_equity_per_instrument_percent, same as every other structure.
        per_contract_reserve = contract.strike * 100.0
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing)
        if quantity < 1:
            return self._result(False,
                                f"Insufficient budget to size cash_secured_put for {self.instrument_name} "
                                f"(strike={contract.strike})")
        reserve = self.account.option_reserve_required("cash_secured_put", quantity, strike=contract.strike)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False,
                                f"Insufficient buying power to reserve {reserve} for cash_secured_put "
                                f"on {self.instrument_name} (available="
                                f"{self.account.available_option_buying_power()})")
        # ONE short put, `quantity` contracts, at contract.strike.
        refusal = self._refuse_if_cannot_take_delivery(
            "cash_secured_put", strike=contract.strike, contracts=quantity)
        if refusal is not None:
            return refusal
        limit_price = contract.bid                          # sell at BID
        leg = OptionLeg(contract_symbol=contract.symbol, side=OrderDirection.SELL,
                        position_intent="sell_to_open", option_type=self.OPTION_TYPE,
                        strike=contract.strike, expiry=contract.expiry, underlying=contract.underlying)
        return self._submit_option_order([leg], quantity, limit_price, "cash_secured_put",
                                         option_reserve=reserve)

    def get_description(self) -> str:
        return f"Sell cash-secured put on {self.instrument_name}"


class OpenBearCallSpreadAction(_OptionEntryAction):
    """Open a bear call (credit) vertical spread: sell lower strike, buy higher strike.

    Short-premium defined-risk bearish structure. SHORT leg is the lower strike
    (sold at BID), LONG leg is the higher strike (bought at ASK as protection).
    net_credit = short.bid - long.ask (must be > 0). The limit price is NEGATIVE
    (Alpaca MLEG convention: negative = net credit). Max loss = (width - net_credit)
    is reserved as buying power. Assignment risk on the short leg if it goes ITM.
    """

    OPTION_TYPE = OptionRight.CALL

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_BEAR_CALL_SPREAD.value

    def _spread_params(self) -> Tuple[Any, Any]:
        """Split strike_param into (long, short) params for the two legs."""
        sp = self.strike_param
        if isinstance(sp, dict):
            return sp.get("long"), sp.get("short")
        if isinstance(sp, (list, tuple)) and len(sp) == 2:
            return sp[0], sp[1]
        # Single value: use the same param for both legs (selector dedups by strike).
        return sp, sp

    def _build_and_submit(self) -> Dict[str, Any]:
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        long_param, short_param = self._spread_params()
        pair = select_vertical_spread(
            chain, method=self.strike_method, long_param=long_param, short_param=short_param,
            spot=spot, option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            **liq)
        if pair is None:
            return self._result(False, f"No liquid bear call spread for {self.instrument_name}")
        # For a CALL spread the selector returns (lo, hi) ordered by strike.
        # Bear CALL CREDIT spread: SHORT = lo (lower strike), LONG = hi (higher strike).
        lo_c, hi_c = pair
        short_c, long_c = lo_c, hi_c
        if short_c.bid is None or long_c.ask is None:
            return self._result(False, f"Missing quote for spread legs on {self.instrument_name}")
        net_credit = round(short_c.bid - long_c.ask, 4)     # sell short@bid, buy long@ask
        if net_credit <= 0:
            return self._result(False,
                                f"Non-positive net credit ({net_credit}) for {self.instrument_name} "
                                f"bear call spread")
        width = round(hi_c.strike - lo_c.strike, 4)
        if width <= 0:
            return self._result(False, f"Non-positive spread width ({width}) for {self.instrument_name}")
        per_spread_reserve = (width - net_credit) * 100.0   # max loss per spread
        if per_spread_reserve <= 0:
            return self._result(False,
                                f"Non-positive max-loss reserve for {self.instrument_name} bear call spread")
        # PREMIUM RICHNESS (OPT-C1). Placed here rather than at the net_credit check two lines
        # up because the collateral is (width - credit), so the width has to be known first.
        refusal = self._refuse_if_arc_below_floor(
            "bear_call_spread", net_credit=net_credit, expiry=short_c.expiry,
            spread_width=width)
        if refusal is not None:
            return refusal
        # Routed through the shared _size_by_reserve() (not inlined) so it also gets capped by
        # max_virtual_equity_per_instrument_percent, same as every other structure.
        quantity = self._size_by_reserve(per_spread_reserve, self.sizing)
        if quantity < 1:
            return self._result(False,
                                f"Insufficient budget to size bear_call_spread for {self.instrument_name} "
                                f"(max_loss={per_spread_reserve})")
        reserve = self.account.option_reserve_required(
            "bear_call_spread", quantity, spread_width=width, net_credit=net_credit)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False,
                                f"Insufficient buying power to reserve {reserve} for bear_call_spread "
                                f"on {self.instrument_name} (available="
                                f"{self.account.available_option_buying_power()})")
        short_leg = OptionLeg(contract_symbol=short_c.symbol, side=OrderDirection.SELL,
                              position_intent="sell_to_open", option_type=self.OPTION_TYPE,
                              strike=short_c.strike, expiry=short_c.expiry, underlying=short_c.underlying)
        long_leg = OptionLeg(contract_symbol=long_c.symbol, side=OrderDirection.BUY,
                             position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                             strike=long_c.strike, expiry=long_c.expiry, underlying=long_c.underlying)
        limit_price = -net_credit                           # NEGATIVE = net credit (Alpaca MLEG)
        return self._submit_option_order([short_leg, long_leg], quantity, limit_price,
                                         "bear_call_spread", option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open bear call spread on {self.instrument_name}"


class OpenBullPutSpreadAction(_OptionEntryAction):
    """Open a bull put (credit) vertical spread: sell the HIGHER strike, buy the LOWER.

    The put mirror of ``OpenBearCallSpreadAction`` and the canonical defined-risk income
    structure — it was the sell arm's only missing directional expression (the searched
    credit residue was one neutral and one bearish structure). SHORT leg is the higher
    strike (sold at BID), LONG leg is the lower strike (bought at ASK as protection).
    net_credit = short.bid - long.ask (must be > 0). The limit price is NEGATIVE (Alpaca
    MLEG convention: negative = net credit). Max loss = (width - net_credit) is reserved
    as buying power. Bullish/neutral: it pays while the underlying stays ABOVE the short
    strike.

    ASSIGNMENT. The short leg is a PUT, so this structure can have shares put to it and
    is charged the full short strike by ``_refuse_if_cannot_take_delivery``. The long
    wing nets NOTHING off that bill — see the comment at the gate below.
    """

    OPTION_TYPE = OptionRight.PUT

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_BULL_PUT_SPREAD.value

    def _spread_params(self) -> Tuple[Any, Any]:
        """Split strike_param into (long, short) params for the two legs."""
        sp = self.strike_param
        if isinstance(sp, dict):
            return sp.get("long"), sp.get("short")
        if isinstance(sp, (list, tuple)) and len(sp) == 2:
            return sp[0], sp[1]
        # Single value: use the same param for both legs (selector dedups by strike).
        return sp, sp

    def _build_and_submit(self) -> Dict[str, Any]:
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        long_param, short_param = self._spread_params()
        pair = select_vertical_spread(
            chain, method=self.strike_method, long_param=long_param, short_param=short_param,
            spot=spot, option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            **liq)
        if pair is None:
            return self._result(False, f"No liquid bull put spread for {self.instrument_name}")
        # For a PUT chain the selector returns its DEBIT ordering: (higher, lower) =
        # (long, short) for a bear put spread. A bull put CREDIT spread is the mirror —
        # SHORT the higher strike, LONG the lower one.
        hi_c, lo_c = pair
        short_c, long_c = hi_c, lo_c
        if short_c.strike is None or long_c.strike is None:
            return self._result(False, f"Missing strike for spread legs on {self.instrument_name}")
        if short_c.strike <= long_c.strike:
            # Long ABOVE short is a bear put DEBIT spread: a different max loss, a
            # different (zero) reserve, and the opposite directional thesis. Opening it
            # under this label would reserve a max loss that does not apply and charge
            # assignment capacity against the wrong leg.
            return self._result(
                False,
                f"Inverted bull put spread for {self.instrument_name}: the short leg "
                f"({short_c.strike}) must be ABOVE the long leg ({long_c.strike}) — a long "
                f"above a short is a bear put DEBIT spread, not a put credit spread")
        if short_c.bid is None or long_c.ask is None:
            return self._result(False, f"Missing quote for spread legs on {self.instrument_name}")
        net_credit = round(short_c.bid - long_c.ask, 4)     # sell short@bid, buy long@ask
        if net_credit <= 0:
            return self._result(False,
                                f"Non-positive net credit ({net_credit}) for {self.instrument_name} "
                                f"bull put spread")
        width = round(short_c.strike - long_c.strike, 4)
        if width <= 0:
            return self._result(False, f"Non-positive spread width ({width}) for {self.instrument_name}")
        per_spread_reserve = (width - net_credit) * 100.0   # max loss per spread
        if per_spread_reserve <= 0:
            return self._result(False,
                                f"Non-positive max-loss reserve for {self.instrument_name} bull put spread")
        # PREMIUM RICHNESS (OPT-C1) — see the bear-call twin: collateral is (width - credit),
        # so this sits after the width rather than beside the net_credit check.
        refusal = self._refuse_if_arc_below_floor(
            "bull_put_spread", net_credit=net_credit, expiry=short_c.expiry,
            spread_width=width)
        if refusal is not None:
            return refusal
        # Routed through the shared _size_by_reserve() (not inlined) so it also gets capped by
        # max_virtual_equity_per_instrument_percent, same as every other structure.
        quantity = self._size_by_reserve(per_spread_reserve, self.sizing)
        if quantity < 1:
            return self._result(False,
                                f"Insufficient budget to size bull_put_spread for {self.instrument_name} "
                                f"(max_loss={per_spread_reserve})")
        reserve = self.account.option_reserve_required(
            "bull_put_spread", quantity, spread_width=width, net_credit=net_credit)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False,
                                f"Insufficient buying power to reserve {reserve} for bull_put_spread "
                                f"on {self.instrument_name} (available="
                                f"{self.account.available_option_buying_power()})")
        # The SHORT PUT leg (the HIGHER strike), `quantity` contracts, charged at its FULL
        # strike. The long put wing nets NOTHING off: the short leg can be assigned
        # TONIGHT, while exercising our own lower-strike put is a choice we make LATER —
        # after the shares have already been paid for. Netting the width here would price
        # a $9,500 obligation at $500 and wave every entry through.
        refusal = self._refuse_if_cannot_take_delivery(
            "bull_put_spread", strike=short_c.strike, contracts=quantity)
        if refusal is not None:
            return refusal
        short_leg = OptionLeg(contract_symbol=short_c.symbol, side=OrderDirection.SELL,
                              position_intent="sell_to_open", option_type=self.OPTION_TYPE,
                              strike=short_c.strike, expiry=short_c.expiry, underlying=short_c.underlying)
        long_leg = OptionLeg(contract_symbol=long_c.symbol, side=OrderDirection.BUY,
                             position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                             strike=long_c.strike, expiry=long_c.expiry, underlying=long_c.underlying)
        limit_price = -net_credit                           # NEGATIVE = net credit (Alpaca MLEG)
        return self._submit_option_order([short_leg, long_leg], quantity, limit_price,
                                         "bull_put_spread", option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open bull put spread on {self.instrument_name}"


class OpenStraddleAction(_OptionEntryAction):
    """Open a long straddle: BUY an ATM call AND an ATM put at the SAME strike.

    Long-volatility, debit structure that profits from a large move in EITHER
    direction (e.g. ahead of earnings). Both legs are bought to open at the strike
    nearest spot, which MUST be identical for the call and the put. net debit =
    call.ask + put.ask (positive); sized by the combined per-contract debit.
    """

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_STRADDLE.value

    def _resolve(self):
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(call_chain, put_chain)
        spot = self._spot()
        # ATM: nearest-spot strike via percent_otm with strike_param=0 on the call chain.
        call_c = select_single(
            call_chain, method="percent_otm", strike_param=0, spot=spot,
            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=None,
            **liq)
        if call_c is None:
            return self._result(False, f"No liquid ATM call for straddle on {self.instrument_name}")
        # Force the put to the SAME strike + expiry as the chosen call leg.
        put_candidates = [c for c in put_chain
                          if c.strike == call_c.strike and c.expiry == call_c.expiry]
        put_c = select_single(
            put_candidates, method="percent_otm", strike_param=0, spot=spot,
            option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=None,
            **liq)
        if put_c is None:
            return self._result(False,
                                f"No liquid ATM put at strike {call_c.strike} for straddle "
                                f"on {self.instrument_name}")
        if call_c.ask is None or put_c.ask is None:
            return self._result(False, f"Missing ask quote for straddle legs on {self.instrument_name}")
        net_debit = round(call_c.ask + put_c.ask, 4)        # buy both at ASK
        if net_debit <= 0:
            return self._result(False,
                                f"Non-positive net debit ({net_debit}) for {self.instrument_name} straddle")
        call_leg = OptionLeg(contract_symbol=call_c.symbol, side=OrderDirection.BUY,
                             position_intent="buy_to_open", option_type=OptionRight.CALL,
                             strike=call_c.strike, expiry=call_c.expiry, underlying=call_c.underlying)
        put_leg = OptionLeg(contract_symbol=put_c.symbol, side=OrderDirection.BUY,
                            position_intent="buy_to_open", option_type=OptionRight.PUT,
                            strike=put_c.strike, expiry=put_c.expiry, underlying=put_c.underlying)
        return ResolvedStructure(
            request=None, legs=[call_leg, put_leg],
            payoff_legs=[PayoffLeg(kind="call", side=OrderDirection.BUY,
                                   premium=call_c.ask, strike=call_c.strike),
                         PayoffLeg(kind="put", side=OrderDirection.BUY,
                                   premium=put_c.ask, strike=put_c.strike)],
            limit_price=net_debit, option_strategy="straddle",
            dte=self._dte_for(call_c.expiry), reserve_per_contract=0.0,
            cost_per_contract=net_debit * 100.0, sizing_basis="premium",
            budget_refusal_message=(
                f"Insufficient budget to size straddle for {self.instrument_name} "
                f"(net_debit={net_debit})"),
            reserve_kwargs={})

    def get_description(self) -> str:
        return f"Open long straddle on {self.instrument_name}"


class OpenStrangleAction(_OptionEntryAction):
    """Open a long strangle: BUY an OTM call AND an OTM put at DIFFERENT strikes.

    Cheaper long-volatility variant of the straddle: the call is bought above spot
    and the put below spot (both OTM by ``strike_param`` percent, default 5%). Both
    legs are bought to open. net debit = call.ask + put.ask (positive); sized by the
    combined per-contract debit. Needs a larger move than a straddle to pay off.
    """

    DEFAULT_OTM_PCT = 5.0   # OTM distance (percent) when strike_param is not configured

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_STRANGLE.value

    def _resolve(self):
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(call_chain, put_chain)
        spot = self._spot()
        otm_pct = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
        call_c = select_single(
            call_chain, method="percent_otm", strike_param=otm_pct, spot=spot,
            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=None,
            **liq)
        if call_c is None:
            return self._result(False, f"No liquid OTM call for strangle on {self.instrument_name}")
        put_c = select_single(
            put_chain, method="percent_otm", strike_param=otm_pct, spot=spot,
            option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=None,
            **liq)
        if put_c is None:
            return self._result(False, f"No liquid OTM put for strangle on {self.instrument_name}")
        if call_c.ask is None or put_c.ask is None:
            return self._result(False, f"Missing ask quote for strangle legs on {self.instrument_name}")
        net_debit = round(call_c.ask + put_c.ask, 4)        # buy both at ASK
        if net_debit <= 0:
            return self._result(False,
                                f"Non-positive net debit ({net_debit}) for {self.instrument_name} strangle")
        call_leg = OptionLeg(contract_symbol=call_c.symbol, side=OrderDirection.BUY,
                             position_intent="buy_to_open", option_type=OptionRight.CALL,
                             strike=call_c.strike, expiry=call_c.expiry, underlying=call_c.underlying)
        put_leg = OptionLeg(contract_symbol=put_c.symbol, side=OrderDirection.BUY,
                            position_intent="buy_to_open", option_type=OptionRight.PUT,
                            strike=put_c.strike, expiry=put_c.expiry, underlying=put_c.underlying)
        return ResolvedStructure(
            request=None, legs=[call_leg, put_leg],
            payoff_legs=[PayoffLeg(kind="call", side=OrderDirection.BUY,
                                   premium=call_c.ask, strike=call_c.strike),
                         PayoffLeg(kind="put", side=OrderDirection.BUY,
                                   premium=put_c.ask, strike=put_c.strike)],
            limit_price=net_debit, option_strategy="strangle",
            dte=self._dte_for(call_c.expiry), reserve_per_contract=0.0,
            cost_per_contract=net_debit * 100.0, sizing_basis="premium",
            budget_refusal_message=(
                f"Insufficient budget to size strangle for {self.instrument_name} "
                f"(net_debit={net_debit})"),
            reserve_kwargs={})

    def get_description(self) -> str:
        return f"Open long strangle on {self.instrument_name}"


class OpenShortStraddleAction(_OptionEntryAction):
    """Short straddle: SELL an ATM call AND an ATM put at the SAME strike (credit).

    Short-volatility: collect both premiums (sold at BID). Net premium is a CREDIT
    (limit price negative). Naked on both sides; reserve the SUM of both legs' Reg-T
    naked margins (the maintenance model's per-leg sum) and size off it."""

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_SHORT_STRADDLE.value

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(call_chain, put_chain)
        spot = self._spot()
        call_c = select_single(
            call_chain, method="percent_otm", strike_param=0, spot=spot,
            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), **liq)
        if call_c is None:
            return self._result(False, f"No liquid ATM call for short straddle on {self.instrument_name}")
        put_candidates = [c for c in put_chain
                          if c.strike == call_c.strike and c.expiry == call_c.expiry]
        put_c = select_single(
            put_candidates, method="percent_otm", strike_param=0, spot=spot,
            option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), **liq)
        if put_c is None:
            return self._result(False, f"No liquid ATM put for short straddle on {self.instrument_name}")
        if call_c.bid is None or put_c.bid is None:
            return self._result(False, f"Missing bid for short straddle legs on {self.instrument_name}")
        net_credit = round(call_c.bid + put_c.bid, 4)        # sell both at BID
        if net_credit <= 0:
            return self._result(False, f"Non-positive credit for {self.instrument_name} short straddle")
        # PREMIUM RICHNESS (OPT-C1). Reserve is the Reg-T naked bracket, so the collateral is
        # much smaller than the delivery bill and the ARC correspondingly larger — which is
        # why the floor for a naked structure is not the floor for a defined-risk one.
        refusal = self._refuse_if_arc_below_floor(
            "short_straddle", net_credit=net_credit, expiry=call_c.expiry,
            strike=call_c.strike, spot=spot)
        if refusal is not None:
            return refusal
        # NAKED both sides: reserve Reg-T naked margin (not full strike*100 cash) so the
        # structure is sizeable on a realistic account. Both legs share the strike; the
        # reserve is the SUM over both rights — the same per-leg sum the backtest's
        # maintenance model charges the open position (review 2026-08-30 F10 sibling;
        # the old GREATER-leg reserve sat below maintenance from the first bar).
        per_contract_reserve = self.account.option_reserve_required(
            "short_straddle", 1, strike=call_c.strike, spot=spot)
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing)
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size short straddle for {self.instrument_name}")
        reserve = self.account.option_reserve_required(
            "short_straddle", quantity, strike=call_c.strike, spot=spot)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for short straddle on {self.instrument_name}")
        # ONE short put leg (put_c), `quantity` contracts. The short CALL at the same
        # strike consumes no PUT-assignment capacity: assigned, it delivers shares and
        # pays cash IN.
        refusal = self._refuse_if_cannot_take_delivery(
            "short_straddle", strike=put_c.strike, contracts=quantity)
        if refusal is not None:
            return refusal
        call_leg = OptionLeg(contract_symbol=call_c.symbol, side=OrderDirection.SELL,
                             position_intent="sell_to_open", option_type=OptionRight.CALL,
                             strike=call_c.strike, expiry=call_c.expiry, underlying=call_c.underlying)
        put_leg = OptionLeg(contract_symbol=put_c.symbol, side=OrderDirection.SELL,
                            position_intent="sell_to_open", option_type=OptionRight.PUT,
                            strike=put_c.strike, expiry=put_c.expiry, underlying=put_c.underlying)
        return self._submit_option_order([call_leg, put_leg], quantity, -net_credit,
                                         "short_straddle", option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open short straddle on {self.instrument_name}"


class OpenShortStrangleAction(_OptionEntryAction):
    """Short strangle: SELL an OTM call AND an OTM put at DIFFERENT strikes (credit).

    Both legs OTM by ``strike_param`` percent (default 10%), sold at BID. Net credit
    (limit negative). Naked both sides; reserve the SUM of both legs' Reg-T naked
    margins (the maintenance model's per-leg sum) and size off it."""

    DEFAULT_OTM_PCT = 10.0

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_SHORT_STRANGLE.value

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(call_chain, put_chain)
        spot = self._spot()
        otm = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
        call_c = select_single(
            call_chain, method="percent_otm", strike_param=otm, spot=spot,
            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), **liq)
        put_c = select_single(
            put_chain, method="percent_otm", strike_param=otm, spot=spot,
            option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), **liq)
        if call_c is None or put_c is None:
            return self._result(False, f"No liquid OTM legs for short strangle on {self.instrument_name}")
        # Pin both legs to the same expiry (use the call's expiry).
        if put_c.expiry != call_c.expiry:
            put_c = select_single(
                [c for c in put_chain if c.expiry == call_c.expiry],
                method="percent_otm", strike_param=otm, spot=spot,
                option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                today=self._today(), **liq)
            if put_c is None:
                return self._result(False, f"No same-expiry OTM put for short strangle on {self.instrument_name}")
        if call_c.bid is None or put_c.bid is None:
            return self._result(False, f"Missing bid for short strangle legs on {self.instrument_name}")
        net_credit = round(call_c.bid + put_c.bid, 4)
        if net_credit <= 0:
            return self._result(False, f"Non-positive credit for {self.instrument_name} short strangle")
        # PREMIUM RICHNESS (OPT-C1). Reserved on BOTH legs' Reg-T brackets, matching the
        # option_reserve_required call below — collateral must be the same number twice.
        refusal = self._refuse_if_arc_below_floor(
            "short_strangle", net_credit=net_credit, expiry=put_c.expiry,
            strike=put_c.strike, call_strike=call_c.strike, spot=spot)
        if refusal is not None:
            return refusal
        # NAKED both sides: reserve Reg-T naked margin for BOTH short legs (not full
        # strike*100 cash), the same per-leg sum the backtest's maintenance model charges
        # the open position — put-leg-only sizing opened ~2x what maintenance tolerates
        # and was instantly force-unwound (review 2026-08-30 F10).
        per_contract_reserve = self.account.option_reserve_required(
            "short_strangle", 1, strike=put_c.strike, call_strike=call_c.strike, spot=spot)
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing)
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size short strangle for {self.instrument_name}")
        reserve = self.account.option_reserve_required(
            "short_strangle", quantity, strike=put_c.strike, call_strike=call_c.strike, spot=spot)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for short strangle on {self.instrument_name}")
        # ONE short put leg (put_c, the lower strike), `quantity` contracts. The short
        # OTM call is not put-assignment capacity.
        refusal = self._refuse_if_cannot_take_delivery(
            "short_strangle", strike=put_c.strike, contracts=quantity)
        if refusal is not None:
            return refusal
        call_leg = OptionLeg(contract_symbol=call_c.symbol, side=OrderDirection.SELL,
                             position_intent="sell_to_open", option_type=OptionRight.CALL,
                             strike=call_c.strike, expiry=call_c.expiry, underlying=call_c.underlying)
        put_leg = OptionLeg(contract_symbol=put_c.symbol, side=OrderDirection.SELL,
                            position_intent="sell_to_open", option_type=OptionRight.PUT,
                            strike=put_c.strike, expiry=put_c.expiry, underlying=put_c.underlying)
        return self._submit_option_order([call_leg, put_leg], quantity, -net_credit,
                                         "short_strangle", option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open short strangle on {self.instrument_name}"


class OpenIronCondorAction(_OptionEntryAction):
    """Iron condor (4 legs, credit, defined risk): SELL OTM put + BUY farther-OTM put
    + SELL OTM call + BUY farther-OTM call. Short legs at ``strike_param`` %OTM; wings
    ``wing_width_pct`` farther OTM. Credit = short bids - long asks (limit negative).
    Max loss = (wing width - credit); reserved per contract."""

    DEFAULT_OTM_PCT = 10.0
    DEFAULT_WING_PCT = 5.0

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_IRON_CONDOR.value

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(call_chain, put_chain)
        spot = self._spot()
        otm = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
        wing = self.wing_width_pct if self.wing_width_pct is not None else self.DEFAULT_WING_PCT
        sc = select_single(call_chain, method="percent_otm", strike_param=otm, spot=spot,
                           option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                           today=self._today(), **liq)
        sp = select_single(put_chain, method="percent_otm", strike_param=otm, spot=spot,
                           option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                           today=self._today(), **liq)
        if sc is None or sp is None:
            return self._result(False, f"No liquid short legs for iron condor on {self.instrument_name}")
        # Wings farther OTM, same expiry as the matching short leg.
        lc = select_wing(call_chain, center_strike=sc.strike, width_pct=wing,
                         option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                         today=self._today(), expiry=sc.expiry,
                         **liq)
        lp = select_wing(put_chain, center_strike=sp.strike, width_pct=wing,
                         option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                         today=self._today(), expiry=sp.expiry,
                         **liq)
        if lc is None or lp is None or lc.strike <= sc.strike or lp.strike >= sp.strike:
            return self._result(False, f"No valid wings for iron condor on {self.instrument_name}")
        if None in (sc.bid, sp.bid, lc.ask, lp.ask):
            return self._result(False, f"Missing quotes for iron condor on {self.instrument_name}")
        net_credit = round(sc.bid + sp.bid - lc.ask - lp.ask, 4)
        if net_credit <= 0:
            return self._result(False, f"Non-positive credit for {self.instrument_name} iron condor")
        width = max(lc.strike - sc.strike, sp.strike - lp.strike)
        # PREMIUM RICHNESS (OPT-C1). After the width, for the same reason as the verticals:
        # a condor's collateral is (wing width - credit).
        refusal = self._refuse_if_arc_below_floor(
            "iron_condor", net_credit=net_credit, expiry=sc.expiry, spread_width=width)
        if refusal is not None:
            return refusal
        max_loss = max(0.0, width - net_credit)
        per_contract_reserve = max_loss * 100.0
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing) if per_contract_reserve > 0 else 0
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size iron condor for {self.instrument_name}")
        reserve = self.account.option_reserve_required(
            "iron_condor", quantity, spread_width=width, net_credit=net_credit)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for iron condor on {self.instrument_name}")
        # The SHORT PUT leg (sp), `quantity` contracts. The long put WING (lp) nets
        # NOTHING off: the short leg can be assigned tonight, while exercising our own
        # put is a choice we make LATER — after the shares have already been paid for.
        # A condor is sized off its wing width, which is why it is the structure this
        # gate bites hardest.
        refusal = self._refuse_if_cannot_take_delivery(
            "iron_condor", strike=sp.strike, contracts=quantity)
        if refusal is not None:
            return refusal
        legs = [
            OptionLeg(contract_symbol=sp.symbol, side=OrderDirection.SELL, position_intent="sell_to_open",
                      option_type=OptionRight.PUT, strike=sp.strike, expiry=sp.expiry, underlying=sp.underlying),
            OptionLeg(contract_symbol=lp.symbol, side=OrderDirection.BUY, position_intent="buy_to_open",
                      option_type=OptionRight.PUT, strike=lp.strike, expiry=lp.expiry, underlying=lp.underlying),
            OptionLeg(contract_symbol=sc.symbol, side=OrderDirection.SELL, position_intent="sell_to_open",
                      option_type=OptionRight.CALL, strike=sc.strike, expiry=sc.expiry, underlying=sc.underlying),
            OptionLeg(contract_symbol=lc.symbol, side=OrderDirection.BUY, position_intent="buy_to_open",
                      option_type=OptionRight.CALL, strike=lc.strike, expiry=lc.expiry, underlying=lc.underlying),
        ]
        return self._submit_option_order(legs, quantity, -net_credit, "iron_condor",
                                         option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open iron condor on {self.instrument_name}"


class OpenJadeLizardAction(_OptionEntryAction):
    """Jade lizard (3 legs, credit): SELL OTM put + SELL OTM call + BUY farther-OTM
    call (caps call-side risk). Short legs at ``strike_param`` %OTM; call wing
    ``wing_width_pct`` farther OTM. Put side remains naked (reserve strike*100).
    Credit = sp.bid + sc.bid - lc.ask (limit negative)."""

    DEFAULT_OTM_PCT = 10.0
    DEFAULT_WING_PCT = 5.0

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_JADE_LIZARD.value

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(call_chain, put_chain)
        spot = self._spot()
        otm = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
        wing = self.wing_width_pct if self.wing_width_pct is not None else self.DEFAULT_WING_PCT
        sc = select_single(call_chain, method="percent_otm", strike_param=otm, spot=spot,
                           option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                           today=self._today(), **liq)
        sp = select_single(put_chain, method="percent_otm", strike_param=otm, spot=spot,
                           option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                           today=self._today(), **liq)
        if sc is None or sp is None:
            return self._result(False, f"No liquid short legs for jade lizard on {self.instrument_name}")
        lc = select_wing(call_chain, center_strike=sc.strike, width_pct=wing,
                         option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                         today=self._today(), expiry=sc.expiry,
                         **liq)
        if lc is None or lc.strike <= sc.strike:
            return self._result(False, f"No valid call wing for jade lizard on {self.instrument_name}")
        if None in (sc.bid, sp.bid, lc.ask):
            return self._result(False, f"Missing quotes for jade lizard on {self.instrument_name}")
        net_credit = round(sc.bid + sp.bid - lc.ask, 4)
        if net_credit <= 0:
            return self._result(False, f"Non-positive credit for {self.instrument_name} jade lizard")
        # Put side is naked, but Alpaca's real MLEG margin engine does NOT grant this 3-leg
        # combo the standard Reg-T naked-margin discount (empirically confirmed exact against a
        # real order: reported cost_basis matched put_strike*100 + call_wing_width*100 -
        # net_credit*100 to the dollar, ~10x the naked_margin_per_contract Reg-T approximation
        # this used to size off — see option_reserve_required's "jade_lizard" branch). Size and
        # reserve off the SAME conservative formula so quantity and the persisted reserve agree
        # with what the broker will actually charge.
        call_wing_width = lc.strike - sc.strike
        # PREMIUM RICHNESS (OPT-C1). Same inputs the reserve is priced from immediately
        # below, so the gate and the sizing agree on the collateral by construction.
        refusal = self._refuse_if_arc_below_floor(
            "jade_lizard", net_credit=net_credit, expiry=sp.expiry,
            strike=sp.strike, spread_width=call_wing_width)
        if refusal is not None:
            return refusal
        per_contract_reserve = self.account.option_reserve_required(
            "jade_lizard", 1, strike=sp.strike, spread_width=call_wing_width, net_credit=net_credit)
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing)
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size jade lizard for {self.instrument_name}")
        reserve = self.account.option_reserve_required(
            "jade_lizard", quantity, strike=sp.strike, spread_width=call_wing_width, net_credit=net_credit)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for jade lizard on {self.instrument_name}")
        # The NAKED short put leg (sp), `quantity` contracts. The other two legs are the
        # call credit spread, which owes shares rather than cash.
        refusal = self._refuse_if_cannot_take_delivery(
            "jade_lizard", strike=sp.strike, contracts=quantity)
        if refusal is not None:
            return refusal
        legs = [
            OptionLeg(contract_symbol=sp.symbol, side=OrderDirection.SELL, position_intent="sell_to_open",
                      option_type=OptionRight.PUT, strike=sp.strike, expiry=sp.expiry, underlying=sp.underlying),
            OptionLeg(contract_symbol=sc.symbol, side=OrderDirection.SELL, position_intent="sell_to_open",
                      option_type=OptionRight.CALL, strike=sc.strike, expiry=sc.expiry, underlying=sc.underlying),
            OptionLeg(contract_symbol=lc.symbol, side=OrderDirection.BUY, position_intent="buy_to_open",
                      option_type=OptionRight.CALL, strike=lc.strike, expiry=lc.expiry, underlying=lc.underlying),
        ]
        return self._submit_option_order(legs, quantity, -net_credit, "jade_lizard",
                                         option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open jade lizard on {self.instrument_name}"


class OpenCallButterflyAction(_OptionEntryAction):
    """Long call butterfly (debit, 1-2-1): BUY 1 lower call + SELL 2 body calls +
    BUY 1 upper call. Body at ``strike_param`` %OTM (~ATM at 0); wings
    ``wing_width_pct`` below/above the body. Net debit = lower.ask + upper.ask
    - 2*body.bid (limit positive). Size off the debit."""

    DEFAULT_BODY_PCT = 0.0
    DEFAULT_WING_PCT = 10.0

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_CALL_BUTTERFLY.value

    def _resolve(self):
        chain = self._chain(OptionRight.CALL)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        body_otm = self.strike_param if self.strike_param is not None else self.DEFAULT_BODY_PCT
        wing = self.wing_width_pct if self.wing_width_pct is not None else self.DEFAULT_WING_PCT
        body = select_single(chain, method="percent_otm", strike_param=body_otm, spot=spot,
                             option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                             today=self._today(), **liq)
        if body is None:
            return self._result(False, f"No liquid body call for butterfly on {self.instrument_name}")
        upper = select_wing(chain, center_strike=body.strike, width_pct=wing,
                            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                            today=self._today(), expiry=body.expiry,
                            **liq)
        # Lower wing: a call BELOW the body. Reuse select_wing with a PUT-style downward
        # target by searching for strike nearest body*(1 - wing%).
        lower_target = body.strike * (1 - wing / 100.0)
        lower_cands = [c for c in chain
                       if c.expiry == body.expiry and c.strike < body.strike
                       and passes_liquidity(c, liq["min_open_interest"],
                                            liq["max_spread_pct"], liq["min_volume"])]
        # option_selector._tie's key, MINUS its expiry term: the candidates above are already
        # pinned to body.expiry (a butterfly with legs in two expiries is a calendar), so an
        # expiry component could never order anything. It was there anyway, reading like a
        # rule that was being enforced — reversing it changed no test in the package suite.
        # Nearest strike wins, then the lower strike, which is all that is left to decide.
        lower = (min(lower_cands, key=lambda c: (abs(c.strike - lower_target), c.strike))
                 if lower_cands else None)
        if upper is None or lower is None or upper.strike <= body.strike or lower.strike >= body.strike:
            return self._result(False, f"No valid wings for butterfly on {self.instrument_name}")
        if None in (lower.ask, upper.ask, body.bid):
            return self._result(False, f"Missing quotes for butterfly on {self.instrument_name}")
        net_debit = round(lower.ask + upper.ask - 2 * body.bid, 4)
        if net_debit <= 0:
            return self._result(False, f"Non-positive debit for {self.instrument_name} butterfly")
        legs = [
            OptionLeg(contract_symbol=lower.symbol, side=OrderDirection.BUY, ratio_qty=1,
                      position_intent="buy_to_open", option_type=OptionRight.CALL,
                      strike=lower.strike, expiry=lower.expiry, underlying=lower.underlying),
            OptionLeg(contract_symbol=body.symbol, side=OrderDirection.SELL, ratio_qty=2,
                      position_intent="sell_to_open", option_type=OptionRight.CALL,
                      strike=body.strike, expiry=body.expiry, underlying=body.underlying),
            OptionLeg(contract_symbol=upper.symbol, side=OrderDirection.BUY, ratio_qty=1,
                      position_intent="buy_to_open", option_type=OptionRight.CALL,
                      strike=upper.strike, expiry=upper.expiry, underlying=upper.underlying),
        ]
        return ResolvedStructure(
            request=None, legs=legs,
            payoff_legs=[PayoffLeg(kind="call", side=OrderDirection.BUY,
                                   premium=lower.ask, strike=lower.strike, ratio=1),
                         PayoffLeg(kind="call", side=OrderDirection.SELL,
                                   premium=body.bid, strike=body.strike, ratio=2),
                         PayoffLeg(kind="call", side=OrderDirection.BUY,
                                   premium=upper.ask, strike=upper.strike, ratio=1)],
            limit_price=net_debit, option_strategy="call_butterfly",
            dte=self._dte_for(body.expiry), reserve_per_contract=0.0,
            cost_per_contract=net_debit * 100.0, sizing_basis="premium",
            budget_refusal_message=(
                f"Insufficient budget to size butterfly for {self.instrument_name}"),
            reserve_kwargs={})

    def get_description(self) -> str:
        return f"Open call butterfly on {self.instrument_name}"


class OpenPutRatioSpreadAction(_OptionEntryAction):
    """Put front-ratio spread (1-2): BUY 1 put near ``strike_param`` %OTM + SELL 2
    puts ``wing_width_pct`` farther OTM. Typically a small credit/even with extra
    downside risk below the short strike. limit = long.ask - 2*short.bid (sign per
    result). The naked short put (1 net short) is reserved at short.strike*100."""

    DEFAULT_OTM_PCT = 5.0
    DEFAULT_WING_PCT = 5.0

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_PUT_RATIO_SPREAD.value

    def _build_and_submit(self) -> Dict[str, Any]:
        chain = self._chain(OptionRight.PUT)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        otm = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
        wing = self.wing_width_pct if self.wing_width_pct is not None else self.DEFAULT_WING_PCT
        long_p = select_single(chain, method="percent_otm", strike_param=otm, spot=spot,
                               option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                               today=self._today(), **liq)
        if long_p is None:
            return self._result(False, f"No liquid long put for ratio spread on {self.instrument_name}")
        short_p = select_wing(chain, center_strike=long_p.strike, width_pct=wing,
                              option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                              today=self._today(), expiry=long_p.expiry,
                              **liq)
        if short_p is None or short_p.strike >= long_p.strike:
            return self._result(False, f"No valid short put wing for ratio spread on {self.instrument_name}")
        if long_p.ask is None or short_p.bid is None:
            return self._result(False, f"Missing quotes for ratio spread on {self.instrument_name}")
        net = round(long_p.ask - 2 * short_p.bid, 4)   # usually negative (credit)
        # Alpaca's real MLEG margin engine does NOT grant Reg-T naked-margin netting (nor any
        # credit for the long leg's partial protection) to this combo shape -- empirically
        # confirmed exact against a real order: it margins the WHOLE position as if it were a
        # plain naked short put at the SHORT strike (cash-secured-style full notional), netted
        # only by the total credit collected (see option_reserve_required's "put_ratio_spread"
        # branch). net_credit is expressed as a positive amount; net>=0 (a net debit, no credit
        # to net against) reserves the full short-strike notional with no discount.
        net_credit = max(0.0, -net)
        # PREMIUM RICHNESS (OPT-C1). This is the structure the gate matters most for: it is
        # "typically a small credit/even" against a full short-strike notional, so a
        # near-zero credit here is the pennies-in-front-of-a-steamroller shape by default.
        # A net DEBIT arrives as net_credit == 0.0, which is a MEASURED 0 %/yr and is refused
        # on its merits by any positive floor rather than for going missing.
        refusal = self._refuse_if_arc_below_floor(
            "put_ratio_spread", net_credit=net_credit, expiry=short_p.expiry,
            strike=short_p.strike)
        if refusal is not None:
            return refusal
        per_contract_reserve = self.account.option_reserve_required(
            "put_ratio_spread", 1, strike=short_p.strike, net_credit=net_credit)
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing)
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size ratio spread for {self.instrument_name}")
        reserve = self.account.option_reserve_required(
            "put_ratio_spread", quantity, strike=short_p.strike, net_credit=net_credit)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for ratio spread on {self.instrument_name}")
        # TWO short puts per structure (`ratio_qty=2` on the short leg below), so the
        # delivery bill is 2 x quantity contracts at the SHORT strike. The single long
        # put at the higher strike nets nothing off, same as the condor's wing.
        refusal = self._refuse_if_cannot_take_delivery(
            "put_ratio_spread", strike=short_p.strike, contracts=2 * quantity)
        if refusal is not None:
            return refusal
        legs = [
            OptionLeg(contract_symbol=long_p.symbol, side=OrderDirection.BUY, ratio_qty=1,
                      position_intent="buy_to_open", option_type=OptionRight.PUT,
                      strike=long_p.strike, expiry=long_p.expiry, underlying=long_p.underlying),
            OptionLeg(contract_symbol=short_p.symbol, side=OrderDirection.SELL, ratio_qty=2,
                      position_intent="sell_to_open", option_type=OptionRight.PUT,
                      strike=short_p.strike, expiry=short_p.expiry, underlying=short_p.underlying),
        ]
        return self._submit_option_order(legs, quantity, net, "put_ratio_spread",
                                         option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open put ratio spread on {self.instrument_name}"


def build_closing_legs(children, parent_quantity: int, quote_fn, held_qty=None) -> "tuple[List[OptionLeg], Optional[float]]":
    """Build reversed legs (and a net limit price) that close a spread's child legs.

    Pure given ``quote_fn`` so it is unit-testable.

    Sign convention matches submit_option_order: net limit >= 0 is a debit (net
    BUY), negative is a credit (net SELL). Each closing leg contributes +ask when
    buying back a short leg and -bid when selling a long leg. Returns
    ``(legs, None)`` when any required quote is missing so the caller can pick a
    fallback price.

    Args:
        children: child TradingOrder rows of the spread parent (contract_symbol set).
        parent_quantity: the parent order quantity (children's ratio is derived from it).
        quote_fn: ``contract_symbol -> OptionQuote | None``.
        held_qty: optional ``{contract_symbol: signed remaining qty}`` netted over the
            transaction's executed option orders. A leg closed individually mid-life is
            FLAT and is SKIPPED — reversing it again would open a NEW opposite position
            (the B10 partial-close hazard); the close ratio is sized from the held qty,
            not the original leg qty. When None every child is closed at its original
            ratio (legacy behavior).
    """
    legs: List[OptionLeg] = []
    net: float = 0.0
    quotes_ok = True
    for child in children:
        if held_qty is not None:
            held = held_qty.get(child.contract_symbol, 0.0)
            if abs(held) < 1e-9:
                continue  # leg closed individually mid-life — nothing left to close
            qty_for_ratio = abs(held)
        else:
            qty_for_ratio = abs(float(child.quantity or 0.0))
        close_side = OrderDirection.SELL if child.side == OrderDirection.BUY else OrderDirection.BUY
        intent = "sell_to_close" if child.side == OrderDirection.BUY else "buy_to_close"
        ratio = 1
        if qty_for_ratio and parent_quantity:
            ratio = max(1, int(round(qty_for_ratio / parent_quantity)))
        legs.append(OptionLeg(
            contract_symbol=child.contract_symbol,
            side=close_side,
            ratio_qty=ratio,
            position_intent=intent,
            option_type=child.option_type,
            strike=child.strike,
            expiry=child.expiry,
            underlying=child.underlying_symbol or child.symbol,
        ))
        quote = quote_fn(child.contract_symbol)
        if close_side == OrderDirection.BUY:
            # Buying back a short leg: pay the ask.
            if quote is None or quote.ask is None:
                quotes_ok = False
            else:
                net += quote.ask * ratio
        else:
            # Selling a long leg: receive the bid.
            if quote is None or quote.bid is None:
                quotes_ok = False
            else:
                net -= quote.bid * ratio
    return legs, (round(net, 4) if quotes_ok else None)


class CloseOptionAction(TradeAction):
    """Close an existing option position via account.close_option_position()."""

    def execute(self) -> "TradeActionResult":
        try:
            if not isinstance(self.account, OptionsAccountInterface):
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE_OPTION.value, success=False,
                    message=f"Account does not support options for {self.instrument_name}", data={})

            order = self._resolve_option_order()
            if order is None:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE_OPTION.value, success=False,
                    message=f"No open option position to close for {self.instrument_name}", data={})

            # Multi-leg (spread) positions: the parent order intentionally has no
            # contract_symbol — closing it as a single leg would submit
            # LimitOrderRequest(symbol=None), which the broker rejects. Reverse the
            # child legs and submit one multi-leg closing order instead.
            if order.contract_symbol is None:
                return self._close_multi_leg(order)

            quantity = order.filled_qty or order.quantity
            avg_entry = order.open_price if order.open_price is not None else order.limit_price
            position = OptionPosition(
                contract_symbol=order.contract_symbol,
                underlying=order.underlying_symbol or order.symbol,
                option_type=order.option_type,
                strike=order.strike,
                expiry=order.expiry,
                side=order.side,
                quantity=abs(float(quantity)) if quantity else 0.0,
                avg_entry_price=avg_entry if avg_entry is not None else 0.0,
            )

            limit_price = self._close_limit_price(position, order)

            if not self.submit_to_broker:
                logger.info(f"CloseOptionAction: submit disabled for {position.contract_symbol} "
                            f"- recording informational result")
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE_OPTION.value, success=True,
                    message=f"Close option deferred for {position.contract_symbol} (manual review, not submitted)",
                    data={"contract_symbol": position.contract_symbol, "limit_price": limit_price,
                          "status": "PENDING"})

            result = self.account.close_option_position(position, order_type="limit", limit_price=limit_price)
            if result is None:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE_OPTION.value, success=False,
                    message=f"Failed to close option position {position.contract_symbol}", data={})
            return self.create_and_save_action_result(
                action_type=ExpertActionType.CLOSE_OPTION.value, success=True,
                message=f"Submitted close for {position.contract_symbol}",
                data={"contract_symbol": position.contract_symbol, "limit_price": limit_price})

        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error executing close_option for {self.instrument_name}: {e}", exc_info=True)
            return self.create_and_save_action_result(
                action_type=ExpertActionType.CLOSE_OPTION.value, success=False,
                message=f"Error executing close option: {str(e)}", data={})

    def _resolve_option_order(self) -> Optional[TradingOrder]:
        """Find the option order to close: prefer existing_order, else the
        transaction's filled option entry order."""
        if self.existing_order is not None and self.existing_order.asset_class == AssetClass.OPTION:
            return self.existing_order
        # Fall back to the OPENED transaction's option entry order
        txn_id = self.existing_order.transaction_id if self.existing_order else None
        if not txn_id:
            return None
        # BT store: TradingOrder is an in-mem model (see trade_store.IN_MEM_MODELS) -- the
        # raw select() below silently finds nothing when the backtest in-mem flag is active
        # (review 2026-07-18, M2). No routed helper covers this asset_class/contract_symbol
        # filter combination, so branch manually (mirrors orders_where's own dual-path shape).
        from ba2_common.core.trade_store import inmem_trades_active, store_all
        if inmem_trades_active():
            orders = [o for o in store_all(TradingOrder)
                     if o.transaction_id == txn_id and o.asset_class == AssetClass.OPTION
                     and o.contract_symbol is not None]
        else:
            from sqlmodel import select, Session
            with Session(get_db().bind) as session:
                orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == txn_id,
                        TradingOrder.asset_class == AssetClass.OPTION,
                        TradingOrder.contract_symbol.is_not(None),
                    )
                ).all()
        for o in orders:
            if o.status in OrderStatus.get_executed_statuses():
                return o
        return orders[0] if orders else None

    def _close_limit_price(self, position: OptionPosition, order: TradingOrder) -> Optional[float]:
        """Long(BUY) closes at the bid; short(SELL) closes at the ask. Use a fresh
        quote when available, else fall back to the entry premium."""
        quote = None
        try:
            quote = self.account.get_option_quote(position.contract_symbol)
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.debug(f"get_option_quote failed for {position.contract_symbol}: {e}")
        if position.side == OrderDirection.BUY:
            if quote is not None and quote.bid is not None:
                return quote.bid
        else:
            if quote is not None and quote.ask is not None:
                return quote.ask
        return order.open_price if order.open_price is not None else order.limit_price

    def _close_multi_leg(self, order: TradingOrder) -> "TradeActionResult":
        """Close a spread position by reversing its child leg orders as one
        multi-leg order. The parent order carries the strategy/transaction; the
        legs carry the contract symbols."""
        # BT store: same M2 fix as _resolve_option_order -- a raw select() bypasses the
        # in-mem store and silently finds nothing when the backtest flag is active.
        from ba2_common.core.trade_store import inmem_trades_active, store_all
        if inmem_trades_active():
            children = [o for o in store_all(TradingOrder)
                       if o.parent_order_id == order.id and o.contract_symbol is not None]
        else:
            from sqlmodel import select, Session
            with Session(get_db().bind) as session:
                children = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.parent_order_id == order.id,
                        TradingOrder.contract_symbol.is_not(None),
                    )
                ).all()
                session.expunge_all()

        if not children:
            return self.create_and_save_action_result(
                action_type=ExpertActionType.CLOSE_OPTION.value, success=False,
                message=f"Spread parent order {order.id} for {self.instrument_name} has no "
                        f"leg orders with contract symbols - cannot build closing order", data={})

        quantity = int(order.filled_qty or order.quantity or 0)
        if quantity < 1:
            return self.create_and_save_action_result(
                action_type=ExpertActionType.CLOSE_OPTION.value, success=False,
                message=f"Spread parent order {order.id} for {self.instrument_name} has no quantity to close",
                data={})

        # Net every contract over the transaction's executed option orders (entry legs +
        # any standalone single-leg closes): a leg closed individually mid-life is FLAT
        # and must not be reversed again (that would OPEN a new opposite position — the
        # B10 partial-close hazard once refresh keeps the transaction OPENED).
        from ba2_common.core.types import OrderStatus
        executed = OrderStatus.get_executed_statuses()
        held_qty: Dict[str, float] = {}
        if inmem_trades_active():
            txn_orders = [o for o in store_all(TradingOrder)
                          if o.transaction_id == order.transaction_id
                          and o.asset_class == AssetClass.OPTION
                          and o.contract_symbol is not None]
        else:
            from sqlmodel import select, Session
            with Session(get_db().bind) as session:
                txn_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == order.transaction_id,
                        TradingOrder.asset_class == AssetClass.OPTION,
                        TradingOrder.contract_symbol.is_not(None),
                    )
                ).all()
                session.expunge_all()
        for o in txn_orders:
            if o.status in executed:
                q = float(o.filled_qty or o.quantity or 0.0)
                held_qty[o.contract_symbol] = held_qty.get(o.contract_symbol, 0.0) + (
                    q if o.side == OrderDirection.BUY else -q)

        legs, net_limit = build_closing_legs(
            children, parent_quantity=quantity, quote_fn=self._safe_option_quote,
            held_qty=held_qty)
        if not legs:
            return self.create_and_save_action_result(
                action_type=ExpertActionType.CLOSE_OPTION.value, success=True,
                message=f"Spread parent order {order.id} for {self.instrument_name}: "
                        f"all legs already closed — nothing left to close",
                data={"contract_symbols": []})
        if net_limit is None:
            # No usable quotes for one or more legs: close at the negated entry
            # premium (entry debit -> closing credit and vice versa) as a neutral
            # fallback rather than refusing to close.
            entry = order.open_price if order.open_price is not None else order.limit_price
            net_limit = -entry if entry is not None else None

        contract_syms = [l.contract_symbol for l in legs]
        if not self.submit_to_broker:
            logger.info(f"CloseOptionAction: submit disabled for spread {contract_syms} "
                        f"- recording informational result")
            return self.create_and_save_action_result(
                action_type=ExpertActionType.CLOSE_OPTION.value, success=True,
                message=f"Close spread deferred for {self.instrument_name} (manual review, not submitted)",
                data={"contract_symbols": contract_syms, "limit_price": net_limit, "status": "PENDING"})

        result = self.account.submit_option_order(
            legs, quantity, order_type="limit", limit_price=net_limit,
            option_strategy="close", transaction_id=order.transaction_id)
        if result is None:
            return self.create_and_save_action_result(
                action_type=ExpertActionType.CLOSE_OPTION.value, success=False,
                message=f"Failed to close spread position for {self.instrument_name} ({contract_syms})",
                data={"contract_symbols": contract_syms})
        return self.create_and_save_action_result(
            action_type=ExpertActionType.CLOSE_OPTION.value, success=True,
            message=f"Submitted multi-leg close for {self.instrument_name} ({contract_syms})",
            data={"contract_symbols": contract_syms, "limit_price": net_limit})

    def _safe_option_quote(self, contract_symbol: str):
        try:
            return self.account.get_option_quote(contract_symbol)
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.debug(f"get_option_quote failed for {contract_symbol}: {e}")
            return None

    def get_description(self) -> str:
        return f"Close option position for {self.instrument_name}"


def create_action(action_type: ExpertActionType, instrument_name: str, account: AccountInterface,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 expert_recommendation: Optional[ExpertRecommendation] = None,
                 **kwargs) -> TradeAction:
    """
    Factory function to create appropriate action based on action type.
    
    Args:
        action_type: Type of action to create
        instrument_name: Instrument name
        account: Account interface
        order_recommendation: Trade recommendation
        existing_order: Optional existing order
        expert_recommendation: Optional expert recommendation for linking
        **kwargs: Additional arguments for specific action types
        
    Returns:
        Appropriate TradeAction instance
    """
    action_map = {
        ExpertActionType.SELL: SellAction,
        ExpertActionType.BUY: BuyAction,
        ExpertActionType.CLOSE: CloseAction,
        ExpertActionType.ADJUST_TAKE_PROFIT: AdjustTakeProfitAction,
        ExpertActionType.ADJUST_STOP_LOSS: AdjustStopLossAction,
        ExpertActionType.INCREASE_INSTRUMENT_SHARE: IncreaseInstrumentShareAction,
        ExpertActionType.DECREASE_INSTRUMENT_SHARE: DecreaseInstrumentShareAction,
        ExpertActionType.BUY_CALL: BuyCallAction,
        ExpertActionType.OPEN_BULL_CALL_SPREAD: OpenBullCallSpreadAction,
        ExpertActionType.SELL_COVERED_CALL: SellCoveredCallAction,
        ExpertActionType.BUY_PUT: BuyPutAction,
        ExpertActionType.OPEN_BEAR_PUT_SPREAD: OpenBearPutSpreadAction,
        ExpertActionType.BUY_PROTECTIVE_PUT: BuyProtectivePutAction,
        ExpertActionType.SELL_CASH_SECURED_PUT: SellCashSecuredPutAction,
        ExpertActionType.OPEN_BEAR_CALL_SPREAD: OpenBearCallSpreadAction,
        ExpertActionType.OPEN_BULL_PUT_SPREAD: OpenBullPutSpreadAction,
        ExpertActionType.OPEN_STRADDLE: OpenStraddleAction,
        ExpertActionType.OPEN_STRANGLE: OpenStrangleAction,
        ExpertActionType.OPEN_SHORT_STRADDLE: OpenShortStraddleAction,
        ExpertActionType.OPEN_SHORT_STRANGLE: OpenShortStrangleAction,
        ExpertActionType.OPEN_IRON_CONDOR: OpenIronCondorAction,
        ExpertActionType.OPEN_JADE_LIZARD: OpenJadeLizardAction,
        ExpertActionType.OPEN_CALL_BUTTERFLY: OpenCallButterflyAction,
        ExpertActionType.OPEN_PUT_RATIO_SPREAD: OpenPutRatioSpreadAction,
        ExpertActionType.CLOSE_OPTION: CloseOptionAction,
    }
    
    action_class = action_map.get(action_type)
    if not action_class:
        raise ValueError(f"Unknown action type: {action_type}")
    
    # Create action with appropriate arguments
    # All actions need expert_recommendation for TradeActionResult linking
    return action_class(instrument_name, account, order_recommendation, existing_order, expert_recommendation, **kwargs)


# TODO: Implement sequence management for complex trading scenarios
# 
# SEQUENCE MANAGEMENT TODO LIST:
# 
# 1. **Order Sequence Manager**: Create a class to manage sequences of dependent actions
#    - Queue multiple actions that need to be executed in order
#    - Wait for order fulfillment before executing next action
#    - Handle partial fills and order rejections
#    - Retry logic for failed actions
# 
# 2. **Order Status Monitoring**: Implement order status tracking
#    - Periodically check order status from broker
#    - Update database records with current status
#    - Trigger next action in sequence when order is filled
#    - Handle timeout scenarios for unfilled orders
# 
# 3. **Bracket Order Support**: Handle complex order types
#    - When opening new position, automatically set TP and SL
#    - Manage OCO (One-Cancels-Other) relationships
#    - Handle order modifications and cancellations
# 
# 4. **Risk Management Integration**: 
#    - Check risk limits before executing each action
#    - Calculate position sizes based on risk parameters
#    - Validate that new orders don't exceed account limits
#    - Emergency stop-loss triggers
# 
# 5. **Event-Driven Architecture**:
#    - Listen for order fill events from broker
#    - Trigger follow-up actions based on order status changes
#    - Handle market data events that might affect pending actions
#    - Integration with job queue system for async processing
# 
# 6. **Error Handling and Recovery**:
#    - Rollback mechanisms for failed sequences
#    - Alert system for critical failures
#    - Manual intervention capabilities
#    - Logging and audit trail for all actions
# 
# 7. **Performance Optimization**:
#    - Batch order submissions where possible
#    - Rate limiting to respect broker API limits
#    - Caching of market data and account information
#    - Efficient database queries for order history
# 
# Example usage scenarios that need sequence management:
# 
# Scenario 1: Open new position with TP/SL
# 1. Submit market buy order
# 2. Wait for fill confirmation
# 3. Create take profit limit order
# 4. Create stop loss order
# 5. Link all orders in database
# 
# Scenario 2: Scale into position
# 1. Submit initial buy order (25% of target position)
# 2. Wait for favorable price movement
# 3. Submit second buy order (25% more)
# 4. Continue until full position is built
# 5. Set TP/SL based on average entry price
# 
# Scenario 3: Dynamic stop loss adjustment
# 1. Monitor position P&L
# 2. When profit reaches certain threshold, move SL to breakeven
# 3. Continue trailing stop as position becomes more profitable
# 4. Handle rapid price movements and ensure orders are updated
# 
# Implementation approach:
# - Use async/await pattern for non-blocking execution
# - Integrate with existing WorkerQueue system
# - Store sequence state in database for persistence
# - Use event-driven callbacks for order status updates
# - Implement timeout and retry mechanisms
# - Add comprehensive logging for debugging and auditing