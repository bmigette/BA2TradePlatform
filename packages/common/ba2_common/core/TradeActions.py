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
from ba2_common.core.option_types import OptionContract, OptionLeg, OptionPosition
from ba2_common.core.option_selector import select_single, select_vertical_spread
from ba2_common.logger import logger


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
            logger.error(f"Error getting current price for {self.instrument_name}: {e}", exc_info=True)
            return None
    
    def get_current_position(self) -> Optional[float]:
        """
        Get current position quantity for the instrument.

        Returns:
            Position quantity (positive for long, negative for short, None if no position)
        """
        try:
            positions = self.account.get_positions()
            for position in positions:
                if hasattr(position, 'symbol') and position.symbol == self.instrument_name:
                    return getattr(position, 'qty', None)
            return None
        except Exception as e:
            logger.error(f"Error getting current position for {self.instrument_name}: {e}", exc_info=True)
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
            from sqlmodel import select, Session
            from ba2_common.core.models import Transaction
            from ba2_common.core.types import TransactionStatus
            from ba2_common.core.db import get_db

            with Session(get_db().bind) as session:
                statement = select(Transaction).where(
                    Transaction.symbol == self.instrument_name,
                    Transaction.expert_id == expert_id,
                    Transaction.status.in_([TransactionStatus.WAITING, TransactionStatus.OPENED]),
                )
                transactions = session.exec(statement).all()

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
            logger.debug(f"Could not copy data from expert recommendation {expert_recommendation_id}: {e}")
            return None
    
    def create_order_record(self, side: str, quantity: float, order_type: str = "market",
                          limit_price: Optional[float] = None, stop_price: Optional[float] = None,
                          linked_order_id: Optional[int] = None,
                          extra_data: Optional[Dict[str, Any]] = None) -> Optional[TradingOrder]:
        """
        Create a TradingOrder database record.

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
            TradingOrder instance or None if creation failed
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
                linked_order_id=linked_order_id,
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
        try:
            # Get current position to validate we can sell
            current_position = self.get_current_position()
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
            current_position = self.get_current_position()
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

                # Apply price calculation based on position direction
                if is_long_position:
                    self.target_price = reference_price * (1 + self.percent / 100)
                    logger.info(f"{self._label} Final (LONG/BUY): ${reference_price:.2f} * (1 + {self.percent:+.2f}/100) = ${self.target_price:.2f}")
                else:
                    self.target_price = reference_price * (1 - self.percent / 100)
                    logger.info(f"{self._label} Final (SHORT/SELL): ${reference_price:.2f} * (1 - {self.percent:+.2f}/100) = ${self.target_price:.2f}")

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

        if is_long:
            return reference_price * (1 + self.percent / 100)
        else:
            return reference_price * (1 - self.percent / 100)

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
                logger.debug(f"Error calculating {self._label} preview: {e}")

        return preview


class AdjustTakeProfitAction(_AdjustPriceLevelAction):
    """Adjust take profit level for an existing order."""

    _action_type = ExpertActionType.ADJUST_TAKE_PROFIT.value
    _label = "TP"
    _long_label = "Take profit"
    _price_key_prefix = "tp"
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

    def _enforce_minimum_distance(self) -> None:
        pass  # TP has no minimum distance enforcement


class AdjustStopLossAction(_AdjustPriceLevelAction):
    """Adjust stop loss level for an existing order."""

    _action_type = ExpertActionType.ADJUST_STOP_LOSS.value
    _label = "SL"
    _long_label = "Stop loss"
    _price_key_prefix = "sl"
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
        return self.account.adjust_sl(transaction, self.target_price, source="ruleset")

    def _post_broker_hook(self, transaction) -> None:
        pass  # SL does not store metadata

    def _enforce_minimum_distance(self) -> None:
        """Enforce minimum SL percent distance from open price."""
        if self.existing_order and self.existing_order.open_price and self.target_price:
            from ba2_common.config import get_min_tp_sl_percent
            min_tp_percent = get_min_tp_sl_percent()

            open_price = float(self.existing_order.open_price)
            is_long = (self.existing_order.side.upper() == "BUY")

            if is_long:
                actual_percent = ((open_price - self.target_price) / open_price) * 100
                if actual_percent < min_tp_percent:
                    enforced_sl = open_price * (1 - min_tp_percent / 100)
                    logger.warning(
                        f"SL enforcement: Maximum loss {actual_percent:.2f}% below minimum {min_tp_percent}%. "
                        f"Adjusting SL from ${self.target_price:.2f} to ${enforced_sl:.2f} (open: ${open_price:.2f})"
                    )
                    self.target_price = enforced_sl
                    self.stop_loss_price = self.target_price
            else:
                actual_percent = ((self.target_price - open_price) / open_price) * 100
                if actual_percent < min_tp_percent:
                    enforced_sl = open_price * (1 + min_tp_percent / 100)
                    logger.warning(
                        f"SL enforcement: Maximum loss {actual_percent:.2f}% below minimum {min_tp_percent}%. "
                        f"Adjusting SL from ${self.target_price:.2f} to ${enforced_sl:.2f} (open: ${open_price:.2f})"
                    )
                    self.target_price = enforced_sl
                    self.stop_loss_price = self.target_price

    def compute_price(self, order: "TradingOrder") -> Optional[float]:
        """Calculate the stop loss price, enforcing minimum distance from open price."""
        price = super().compute_price(order)

        # Enforce minimum SL distance from open price
        if price is not None and order.open_price:
            from ba2_common.config import get_min_tp_sl_percent
            min_pct = get_min_tp_sl_percent()
            open_price = float(order.open_price)

            if self.order_recommendation in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                is_long = True
            elif self.order_recommendation in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                is_long = False
            else:
                order_side = str(order.side.value if hasattr(order.side, 'value') else order.side).upper()
                is_long = (order_side == "BUY")

            if is_long:
                actual_pct = ((open_price - price) / open_price) * 100
                if actual_pct < min_pct:
                    price = open_price * (1 - min_pct / 100)
            else:
                actual_pct = ((price - open_price) / open_price) * 100
                if actual_pct < min_pct:
                    price = open_price * (1 + min_pct / 100)

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

            # Check available balance
            account_balance = self.account.get_account_info().get('buying_power', 0)
            if additional_value > account_balance:
                logger.warning(f"Additional value ${additional_value:.2f} exceeds available balance ${account_balance:.2f}")
                additional_value = account_balance

            # Calculate additional quantity needed
            additional_qty = additional_value / current_price

            # Round to appropriate lot size (minimum 1 share)
            additional_qty = max(1.0, round(additional_qty))

            logger.info(f"Increasing {self.instrument_name}: expert_qty={current_position_qty}, additional={additional_qty}, "
                       f"target_value=${target_value:.2f} ({self.target_percent}% of ${virtual_equity:.2f})")

            # Determine side based on current position or recommendation
            if current_position_qty >= 0:
                side = "BUY"
            else:
                side = "SELL"  # Short position - sell more
            
            # Create market order
            order = self.create_order_record(
                side=side,
                quantity=additional_qty,
                order_type="market"
            )
            
            if not order:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Failed to create order record",
                    data={}
                )
            
            # Save order to database
            order_id = add_instance(order)
            if not order_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Failed to save order to database",
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

            # Calculate quantity to sell
            reduction_qty = reduction_value / current_price

            # Round appropriately
            reduction_qty = round(reduction_qty)

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
            
            # Create market order
            order = self.create_order_record(
                side=side,
                quantity=reduction_qty,
                order_type="market"
            )
            
            if not order:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Failed to create order record",
                    data={}
                )
            
            # Save order to database
            order_id = add_instance(order)
            if not order_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.DECREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Failed to save order to database",
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
    sizing, and the submit_to_broker gate. Concrete subclasses implement
    `_build_and_submit()` which selects contract(s), builds legs, computes the
    limit premium (buy@ask / sell@bid), sizes the order, and submits.
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
                 wing_width_pct: Optional[float] = None,
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
        self.wing_width_pct = wing_width_pct

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
            logger.debug(f"_spot mid lookup failed for {self.instrument_name}: {e}")
        return self.get_current_price()

    def _chain(self, option_type: OptionRight) -> List[OptionContract]:
        today = self._today()
        expiry_min = today + timedelta(days=self.dte_min) if self.dte_min is not None else today
        expiry_max = today + timedelta(days=self.dte_max) if self.dte_max is not None else today
        return self.account.get_option_chain(
            self.instrument_name, expiry_min, expiry_max, option_type)

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
                logger.debug(f"_virtual_equity: could not load ExpertInstance {instance_id}: {e}")
        return balance * (pct / 100.0)

    def _size(self, premium: float, sizing_pct: Optional[float]) -> int:
        """floor(virtual_equity * sizing% / (premium * 100)); 0 if not sizeable."""
        if premium is None or premium <= 0 or not sizing_pct or sizing_pct <= 0:
            return 0
        equity = self._virtual_equity()
        if equity is None or equity <= 0:
            return 0
        budget = equity * (sizing_pct / 100.0)
        return int(math.floor(budget / (premium * 100.0)))

    def _size_by_reserve(self, reserve_per_contract: float,
                         sizing_pct: Optional[float]) -> int:
        """floor(virtual_equity * sizing% / reserve_per_contract). For credit/naked
        structures where net premium is negative (can't size off premium)."""
        if not reserve_per_contract or reserve_per_contract <= 0:
            return 0
        if not sizing_pct or sizing_pct <= 0:
            return 0
        equity = self._virtual_equity()
        if equity is None or equity <= 0:
            return 0
        return int(math.floor((equity * (sizing_pct / 100.0)) / reserve_per_contract))

    def _held_equity_shares(self) -> float:
        """Sum filled equity BUY quantity across this expert's OPENED transactions for the symbol."""
        instance_id = self.expert_recommendation.instance_id if self.expert_recommendation else None
        if not instance_id:
            return 0.0
        from sqlmodel import select, Session
        from ba2_common.core.models import Transaction
        total = 0.0
        with Session(get_db().bind) as session:
            txns = session.exec(
                select(Transaction).where(
                    Transaction.symbol == self.instrument_name,
                    Transaction.expert_id == instance_id,
                    Transaction.status == TransactionStatus.OPENED,
                )
            ).all()
            txn_ids = [t.id for t in txns]
            if not txn_ids:
                return 0.0
            orders = session.exec(
                select(TradingOrder).where(TradingOrder.transaction_id.in_(txn_ids))
            ).all()
            for o in orders:
                if o.asset_class == AssetClass.OPTION:
                    continue
                if o.status not in OrderStatus.get_executed_statuses():
                    continue
                qty = o.filled_qty
                if not qty:
                    continue
                if o.side == OrderDirection.BUY:
                    total += abs(float(qty))
                else:
                    total -= abs(float(qty))
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

    def _submit_option_order(self, legs: List[OptionLeg], quantity: int,
                             limit_price: float, option_strategy: str,
                             option_reserve: Optional[float] = None) -> Dict[str, Any]:
        """Submit (or defer) the assembled option order, honoring submit_to_broker.

        When `option_reserve` is provided (short-premium strategies: CSP / credit
        spread), it is persisted on the parent order's `data["option_reserve"]` so
        `OptionsAccountInterface.reserved_option_buying_power()` can account for it.
        """
        expert_rec_id = self.expert_recommendation.id if self.expert_recommendation else None
        data = {
            "option_strategy": option_strategy,
            "quantity": quantity,
            "limit_price": limit_price,
            "legs": [{"contract_symbol": leg.contract_symbol, "side": leg.side.value,
                      "position_intent": leg.position_intent, "strike": leg.strike}
                     for leg in legs],
        }
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
                logger.error(f"Failed to persist option_reserve on order {order_id}: {e}", exc_info=True)
        return self._result(True, f"Submitted {option_strategy} for {self.instrument_name}", data)

    def _build_and_submit(self) -> Dict[str, Any]:
        raise NotImplementedError

    def execute(self) -> "TradeActionResult":
        try:
            if not self._supports_options():
                return self._result(False, f"Account does not support options for {self.instrument_name}")
            return self._build_and_submit()
        except Exception as e:
            logger.error(f"Error executing {self._action_type_value()} for {self.instrument_name}: {e}",
                         exc_info=True)
            return self._result(False, f"Error executing option action: {str(e)}")


class BuyCallAction(_OptionEntryAction):
    """Buy a single long call (debit) selected from the chain."""

    OPTION_TYPE = OptionRight.CALL

    def _action_type_value(self) -> str:
        return ExpertActionType.BUY_CALL.value

    def _build_and_submit(self) -> Dict[str, Any]:
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        contract = select_single(
            chain, method=self.strike_method, strike_param=self.strike_param, spot=spot,
            option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        if contract is None:
            return self._result(False, f"No liquid call contract for {self.instrument_name}")
        if contract.ask is None or contract.ask <= 0:
            return self._result(False, f"No ask price for {contract.symbol}")
        limit_price = contract.ask                          # buy at ASK
        quantity = self._size(limit_price, self.sizing)
        if quantity < 1:
            return self._result(False,
                                f"Insufficient budget to size long_call for {self.instrument_name} "
                                f"(premium={limit_price})")
        leg = OptionLeg(contract_symbol=contract.symbol, side=OrderDirection.BUY,
                        position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                        strike=contract.strike, expiry=contract.expiry, underlying=contract.underlying)
        return self._submit_option_order([leg], quantity, limit_price, "long_call")

    def get_description(self) -> str:
        return f"Buy long call on {self.instrument_name}"


class OpenBullCallSpreadAction(_OptionEntryAction):
    """Open a bull call (debit) vertical spread: buy lower strike, sell higher strike."""

    OPTION_TYPE = OptionRight.CALL

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_BULL_CALL_SPREAD.value

    def _build_and_submit(self) -> Dict[str, Any]:
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        long_param, short_param = self._spread_params()
        pair = select_vertical_spread(
            chain, method=self.strike_method, long_param=long_param, short_param=short_param,
            spot=spot, option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        if pair is None:
            return self._result(False, f"No liquid bull call spread for {self.instrument_name}")
        long_c, short_c = pair
        if long_c.ask is None or short_c.bid is None:
            return self._result(False, f"Missing quote for spread legs on {self.instrument_name}")
        net_debit = round(long_c.ask - short_c.bid, 4)      # buy long@ask, sell short@bid
        if net_debit <= 0:
            return self._result(False,
                                f"Non-positive net debit ({net_debit}) for {self.instrument_name} spread")
        quantity = self._size(net_debit, self.sizing)
        if quantity < 1:
            return self._result(False,
                                f"Insufficient budget to size bull_call_spread for {self.instrument_name} "
                                f"(net_debit={net_debit})")
        long_leg = OptionLeg(contract_symbol=long_c.symbol, side=OrderDirection.BUY,
                             position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                             strike=long_c.strike, expiry=long_c.expiry, underlying=long_c.underlying)
        short_leg = OptionLeg(contract_symbol=short_c.symbol, side=OrderDirection.SELL,
                              position_intent="sell_to_open", option_type=self.OPTION_TYPE,
                              strike=short_c.strike, expiry=short_c.expiry, underlying=short_c.underlying)
        return self._submit_option_order([long_leg, short_leg], quantity, net_debit, "bull_call_spread")

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

    def _build_and_submit(self) -> Dict[str, Any]:
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        contract = select_single(
            chain, method=self.strike_method, strike_param=self.strike_param, spot=spot,
            option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        if contract is None:
            return self._result(False, f"No liquid put contract for {self.instrument_name}")
        if contract.ask is None or contract.ask <= 0:
            return self._result(False, f"No ask price for {contract.symbol}")
        limit_price = contract.ask                          # buy at ASK
        quantity = self._size(limit_price, self.sizing)
        if quantity < 1:
            return self._result(False,
                                f"Insufficient budget to size long_put for {self.instrument_name} "
                                f"(premium={limit_price})")
        leg = OptionLeg(contract_symbol=contract.symbol, side=OrderDirection.BUY,
                        position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                        strike=contract.strike, expiry=contract.expiry, underlying=contract.underlying)
        return self._submit_option_order([leg], quantity, limit_price, "long_put")

    def get_description(self) -> str:
        return f"Buy long put on {self.instrument_name}"


class OpenBearPutSpreadAction(_OptionEntryAction):
    """Open a bear put (debit) vertical spread: buy higher strike, sell lower strike."""

    OPTION_TYPE = OptionRight.PUT

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_BEAR_PUT_SPREAD.value

    def _build_and_submit(self) -> Dict[str, Any]:
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        long_param, short_param = self._spread_params()
        pair = select_vertical_spread(
            chain, method=self.strike_method, long_param=long_param, short_param=short_param,
            spot=spot, option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
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
        quantity = self._size(net_debit, self.sizing)
        if quantity < 1:
            return self._result(False,
                                f"Insufficient budget to size bear_put_spread for {self.instrument_name} "
                                f"(net_debit={net_debit})")
        long_leg = OptionLeg(contract_symbol=long_c.symbol, side=OrderDirection.BUY,
                             position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                             strike=long_c.strike, expiry=long_c.expiry, underlying=long_c.underlying)
        short_leg = OptionLeg(contract_symbol=short_c.symbol, side=OrderDirection.SELL,
                              position_intent="sell_to_open", option_type=self.OPTION_TYPE,
                              strike=short_c.strike, expiry=short_c.expiry, underlying=short_c.underlying)
        return self._submit_option_order([long_leg, short_leg], quantity, net_debit, "bear_put_spread")

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
        quantity = int(math.floor(held / 100.0)) if held > 0 else 0
        if quantity < 1:
            return self._result(False,
                                f"Held equity below one contract lot for covered call on {self.instrument_name} "
                                f"(shares={held}, 100 required per contract) - size the equity BUY with "
                                f"lot_size=100 or pick a cheaper underlying")
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        contract = select_single(
            chain, method=self.strike_method, strike_param=self.strike_param, spot=spot,
            option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        if contract is None:
            return self._result(False, f"No liquid call contract for covered call on {self.instrument_name}")
        if contract.bid is None or contract.bid <= 0:
            return self._result(False, f"No bid price for {contract.symbol}")
        limit_price = contract.bid                          # sell at BID
        leg = OptionLeg(contract_symbol=contract.symbol, side=OrderDirection.SELL,
                        position_intent="sell_to_open", option_type=self.OPTION_TYPE,
                        strike=contract.strike, expiry=contract.expiry, underlying=contract.underlying)
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
        quantity = int(math.floor(held / 100.0)) if held > 0 else 0
        if quantity < 1:
            return self._result(False,
                                f"Held equity below one contract lot for protective put on {self.instrument_name} "
                                f"(shares={held}, 100 required per contract) - size the equity BUY with "
                                f"lot_size=100 or pick a cheaper underlying")
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        contract = select_single(
            chain, method=self.strike_method, strike_param=self.strike_param, spot=spot,
            option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
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
        spot = self._spot()
        contract = select_single(
            chain, method=self.strike_method, strike_param=self.strike_param, spot=spot,
            option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        if contract is None:
            return self._result(False, f"No liquid put contract for cash-secured put on {self.instrument_name}")
        if contract.bid is None or contract.bid <= 0:
            return self._result(False, f"No bid price for {contract.symbol}")
        if contract.strike is None or contract.strike <= 0:
            return self._result(False, f"No strike for {contract.symbol}")
        # Sizing: budget by the cash that must be reserved (strike*100), not the premium.
        equity = self._virtual_equity()
        if equity is None or equity <= 0:
            return self._result(False, f"No virtual equity available for {self.instrument_name}")
        if not self.sizing or self.sizing <= 0:
            return self._result(False, f"No sizing configured for cash-secured put on {self.instrument_name}")
        budget = equity * (self.sizing / 100.0)
        per_contract_reserve = contract.strike * 100.0
        quantity = int(math.floor(budget / per_contract_reserve))
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
        spot = self._spot()
        long_param, short_param = self._spread_params()
        pair = select_vertical_spread(
            chain, method=self.strike_method, long_param=long_param, short_param=short_param,
            spot=spot, option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(),
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
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
        equity = self._virtual_equity()
        if equity is None or equity <= 0:
            return self._result(False, f"No virtual equity available for {self.instrument_name}")
        if not self.sizing or self.sizing <= 0:
            return self._result(False, f"No sizing configured for bear call spread on {self.instrument_name}")
        budget = equity * (self.sizing / 100.0)
        quantity = int(math.floor(budget / per_spread_reserve))
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


class OpenStraddleAction(_OptionEntryAction):
    """Open a long straddle: BUY an ATM call AND an ATM put at the SAME strike.

    Long-volatility, debit structure that profits from a large move in EITHER
    direction (e.g. ahead of earnings). Both legs are bought to open at the strike
    nearest spot, which MUST be identical for the call and the put. net debit =
    call.ask + put.ask (positive); sized by the combined per-contract debit.
    """

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_STRADDLE.value

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        # ATM: nearest-spot strike via percent_otm with strike_param=0 on the call chain.
        call_c = select_single(
            call_chain, method="percent_otm", strike_param=0, spot=spot,
            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=None,
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        if call_c is None:
            return self._result(False, f"No liquid ATM call for straddle on {self.instrument_name}")
        # Force the put to the SAME strike + expiry as the chosen call leg.
        put_candidates = [c for c in put_chain
                          if c.strike == call_c.strike and c.expiry == call_c.expiry]
        put_c = select_single(
            put_candidates, method="percent_otm", strike_param=0, spot=spot,
            option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=None,
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
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
        quantity = self._size(net_debit, self.sizing)
        if quantity < 1:
            return self._result(False,
                                f"Insufficient budget to size straddle for {self.instrument_name} "
                                f"(net_debit={net_debit})")
        call_leg = OptionLeg(contract_symbol=call_c.symbol, side=OrderDirection.BUY,
                             position_intent="buy_to_open", option_type=OptionRight.CALL,
                             strike=call_c.strike, expiry=call_c.expiry, underlying=call_c.underlying)
        put_leg = OptionLeg(contract_symbol=put_c.symbol, side=OrderDirection.BUY,
                            position_intent="buy_to_open", option_type=OptionRight.PUT,
                            strike=put_c.strike, expiry=put_c.expiry, underlying=put_c.underlying)
        return self._submit_option_order([call_leg, put_leg], quantity, net_debit, "straddle")

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

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        otm_pct = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
        call_c = select_single(
            call_chain, method="percent_otm", strike_param=otm_pct, spot=spot,
            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=None,
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        if call_c is None:
            return self._result(False, f"No liquid OTM call for strangle on {self.instrument_name}")
        put_c = select_single(
            put_chain, method="percent_otm", strike_param=otm_pct, spot=spot,
            option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=None,
            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        if put_c is None:
            return self._result(False, f"No liquid OTM put for strangle on {self.instrument_name}")
        if call_c.ask is None or put_c.ask is None:
            return self._result(False, f"Missing ask quote for strangle legs on {self.instrument_name}")
        net_debit = round(call_c.ask + put_c.ask, 4)        # buy both at ASK
        if net_debit <= 0:
            return self._result(False,
                                f"Non-positive net debit ({net_debit}) for {self.instrument_name} strangle")
        quantity = self._size(net_debit, self.sizing)
        if quantity < 1:
            return self._result(False,
                                f"Insufficient budget to size strangle for {self.instrument_name} "
                                f"(net_debit={net_debit})")
        call_leg = OptionLeg(contract_symbol=call_c.symbol, side=OrderDirection.BUY,
                             position_intent="buy_to_open", option_type=OptionRight.CALL,
                             strike=call_c.strike, expiry=call_c.expiry, underlying=call_c.underlying)
        put_leg = OptionLeg(contract_symbol=put_c.symbol, side=OrderDirection.BUY,
                            position_intent="buy_to_open", option_type=OptionRight.PUT,
                            strike=put_c.strike, expiry=put_c.expiry, underlying=put_c.underlying)
        return self._submit_option_order([call_leg, put_leg], quantity, net_debit, "strangle")

    def get_description(self) -> str:
        return f"Open long strangle on {self.instrument_name}"


class OpenShortStraddleAction(_OptionEntryAction):
    """Short straddle: SELL an ATM call AND an ATM put at the SAME strike (credit).

    Short-volatility: collect both premiums (sold at BID). Net premium is a CREDIT
    (limit price negative). Naked on both sides; reserve a conservative strike*100
    per contract proxy and size off it."""

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_SHORT_STRADDLE.value

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        call_c = select_single(
            call_chain, method="percent_otm", strike_param=0, spot=spot,
            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), min_open_interest=self.min_open_interest,
            max_spread_pct=self.max_spread_pct)
        if call_c is None:
            return self._result(False, f"No liquid ATM call for short straddle on {self.instrument_name}")
        put_candidates = [c for c in put_chain
                          if c.strike == call_c.strike and c.expiry == call_c.expiry]
        put_c = select_single(
            put_candidates, method="percent_otm", strike_param=0, spot=spot,
            option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), min_open_interest=self.min_open_interest,
            max_spread_pct=self.max_spread_pct)
        if put_c is None:
            return self._result(False, f"No liquid ATM put for short straddle on {self.instrument_name}")
        if call_c.bid is None or put_c.bid is None:
            return self._result(False, f"Missing bid for short straddle legs on {self.instrument_name}")
        net_credit = round(call_c.bid + put_c.bid, 4)        # sell both at BID
        if net_credit <= 0:
            return self._result(False, f"Non-positive credit for {self.instrument_name} short straddle")
        per_contract_reserve = call_c.strike * 100.0
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing)
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size short straddle for {self.instrument_name}")
        reserve = self.account.option_reserve_required(
            "short_straddle", quantity, strike=call_c.strike)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for short straddle on {self.instrument_name}")
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
    (limit negative). Naked both sides; reserve strike*100 of the SHORT PUT per
    contract proxy and size off it."""

    DEFAULT_OTM_PCT = 10.0

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_SHORT_STRANGLE.value

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        otm = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
        call_c = select_single(
            call_chain, method="percent_otm", strike_param=otm, spot=spot,
            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), min_open_interest=self.min_open_interest,
            max_spread_pct=self.max_spread_pct)
        put_c = select_single(
            put_chain, method="percent_otm", strike_param=otm, spot=spot,
            option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), min_open_interest=self.min_open_interest,
            max_spread_pct=self.max_spread_pct)
        if call_c is None or put_c is None:
            return self._result(False, f"No liquid OTM legs for short strangle on {self.instrument_name}")
        # Pin both legs to the same expiry (use the call's expiry).
        if put_c.expiry != call_c.expiry:
            put_c = select_single(
                [c for c in put_chain if c.expiry == call_c.expiry],
                method="percent_otm", strike_param=otm, spot=spot,
                option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                today=self._today(), min_open_interest=self.min_open_interest,
                max_spread_pct=self.max_spread_pct)
            if put_c is None:
                return self._result(False, f"No same-expiry OTM put for short strangle on {self.instrument_name}")
        if call_c.bid is None or put_c.bid is None:
            return self._result(False, f"Missing bid for short strangle legs on {self.instrument_name}")
        net_credit = round(call_c.bid + put_c.bid, 4)
        if net_credit <= 0:
            return self._result(False, f"Non-positive credit for {self.instrument_name} short strangle")
        per_contract_reserve = put_c.strike * 100.0
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing)
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size short strangle for {self.instrument_name}")
        reserve = self.account.option_reserve_required(
            "short_strangle", quantity, strike=put_c.strike)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for short strangle on {self.instrument_name}")
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


def build_closing_legs(children, parent_quantity: int, quote_fn) -> "tuple[List[OptionLeg], Optional[float]]":
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
    """
    legs: List[OptionLeg] = []
    net: float = 0.0
    quotes_ok = True
    for child in children:
        close_side = OrderDirection.SELL if child.side == OrderDirection.BUY else OrderDirection.BUY
        intent = "sell_to_close" if child.side == OrderDirection.BUY else "buy_to_close"
        ratio = 1
        if child.quantity and parent_quantity:
            ratio = max(1, int(round(abs(child.quantity) / parent_quantity)))
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

        legs, net_limit = build_closing_legs(
            children, parent_quantity=quantity, quote_fn=self._safe_option_quote)
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
        ExpertActionType.OPEN_STRADDLE: OpenStraddleAction,
        ExpertActionType.OPEN_STRANGLE: OpenStrangleAction,
        ExpertActionType.OPEN_SHORT_STRADDLE: OpenShortStraddleAction,
        ExpertActionType.OPEN_SHORT_STRANGLE: OpenShortStrangleAction,
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