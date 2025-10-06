"""
TradeActions - Core component for executing trading actions

This module provides base classes and implementations for executing various trading actions
based on expert recommendations and market conditions.
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import uuid

from .AccountInterface import AccountInterface
from .models import TradingOrder, ExpertRecommendation, TradeActionResult
from .types import OrderRecommendation, ExpertActionType, OrderDirection, OrderType, OrderStatus
from .db import get_db, add_instance, update_instance, get_instance
from ..logger import logger


class TradeAction(ABC):
    """
    Base class for all trading actions.
    
    Provides common functionality for executing trading actions based on:
    - Account interface
    - Instrument information
    - Order recommendations
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
    
    def create_order_record(self, side: str, quantity: float, order_type: str = "market", 
                          limit_price: Optional[float] = None, stop_price: Optional[float] = None,
                          linked_order_id: Optional[int] = None) -> Optional[TradingOrder]:
        """
        Create a TradingOrder database record.
        
        Args:
            side: Order side ("buy" or "sell", case-insensitive)
            quantity: Order quantity
            order_type: Order type ("market", "limit", "stop", etc.)
            limit_price: Limit price for limit orders
            stop_price: Stop price for stop orders
            linked_order_id: ID of linked order (for TP/SL orders)
            
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
                from .db import get_instance
                from .models import ExpertRecommendation
                expert_rec = get_instance(ExpertRecommendation, expert_recommendation_id)
                if expert_rec:
                    expert_instance_id = expert_rec.instance_id
                    comment_parts.append(f"TR:{expert_instance_id}")
                    comment_parts.append(f"REC:{expert_recommendation_id}")
            
            comment = f"[{'/'.join(comment_parts)}]"
            
            # Determine open_type: AUTOMATIC for TP/SL orders, otherwise from expert_recommendation presence
            from .types import OrderOpenType
            if linked_order_id is not None:
                # This is a TP/SL order (has a linked parent order)
                open_type = OrderOpenType.AUTOMATIC
            elif expert_recommendation_id is not None:
                # Order created from expert recommendation
                open_type = OrderOpenType.AUTOMATIC
            else:
                # Manual order
                open_type = OrderOpenType.MANUAL
            
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
                created_at=datetime.now(timezone.utc)
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
    
    def create_action_result(self, action_type: str, success: bool, message: str, 
                           data: Optional[Dict[str, Any]] = None,
                           transaction_id: Optional[int] = None,
                           expert_recommendation_id: Optional[int] = None) -> "TradeActionResult":
        """
        Create a TradeActionResult instance for this action.
        
        Args:
            action_type: Type of action (buy, sell, close, etc.)
            success: Whether the action was successful
            message: Human-readable message about the result
            data: Additional data from the action
            transaction_id: Optional transaction ID to link
            expert_recommendation_id: Optional expert recommendation ID to link
            
        Returns:
            TradeActionResult instance (not yet saved to database)
        """
        if data is None:
            data = {}
            
        result = TradeActionResult(
            action_type=action_type,
            success=success,
            message=message,
            data=data,
            transaction_id=transaction_id,
            expert_recommendation_id=expert_recommendation_id
        )
        
        return result
    
    def create_and_save_action_result(self, action_type: str, success: bool, message: str, 
                                       data: Optional[Dict[str, Any]] = None,
                                       transaction_id: Optional[int] = None,
                                       expert_recommendation_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Create and save a TradeActionResult, returning a dictionary of attributes.
        
        This method creates a result, saves it to the database, and returns a dict
        to avoid DetachedInstanceError when accessing attributes.
        
        Args:
            action_type: Type of action (buy, sell, close, etc.)
            success: Whether the action was successful
            message: Human-readable message about the result
            data: Additional data dictionary
            transaction_id: Optional transaction ID
            expert_recommendation_id: Optional expert recommendation ID
            
        Returns:
            Dictionary with result attributes (id, action_type, success, message, data)
        """
        if data is None:
            data = {}
        
        # Create the result object
        result = TradeActionResult(
            action_type=action_type,
            success=success,
            message=message,
            data=data,
            transaction_id=transaction_id,
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
            'transaction_id': transaction_id,
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
                order_type="market"
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
        
        Returns:
            TradeActionResult object containing execution results
        """
        try:
            current_position = self.get_current_position()
            if current_position is None or current_position == 0:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE.value,
                    success=False,
                    message=f"No position to close for {self.instrument_name}",
                    data={}
                )
            
            # Determine order side based on current position
            if current_position > 0:
                # Long position - sell to close
                side = "sell"
                quantity = current_position
            else:
                # Short position - buy to cover
                side = "buy"
                quantity = abs(current_position)
            
            # Create order record
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
            
            # Retrieve the order object for submission (needs to be in a session)
            order_record = get_instance(TradingOrder, order_id)
            if not order_record:
                logger.error(f"Failed to retrieve order {order_id} after creation")
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE.value,
                    success=False,
                    message="Failed to retrieve order record",
                    data={}
                )
            
            # Submit order through account interface
            submit_result = self.account.submit_order(order_record)
            
            if submit_result is not None:
                # Update order record with broker order ID
                if hasattr(submit_result, 'account_order_id') and submit_result.account_order_id:
                    order_record.broker_order_id = str(submit_result.account_order_id)
                    order_record.status = OrderStatus.OPEN.value
                    update_instance(order_record)
                
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.CLOSE.value,
                    success=True,
                    message=f"Close order submitted for {self.instrument_name}",
                    data={"order_id": order_id, "broker_order_id": submit_result.account_order_id}
                )
            else:
                # Update order status to failed
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


class AdjustTakeProfitAction(TradeAction):
    """Adjust take profit level for an existing order."""
    
    def __init__(self, instrument_name: str, account: AccountInterface, 
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 take_profit_price: Optional[float] = None,
                 reference_value: Optional[str] = None, percent: Optional[float] = None):
        """
        Initialize adjust take profit action.
        
        Args:
            instrument_name: Instrument name
            account: Account interface
            order_recommendation: Order recommendation
            existing_order: Existing order to adjust (required - from enter_market or open position)
            take_profit_price: New take profit price (if provided directly)
            reference_value: Reference price type ('order_open_price', 'current_price', 'expert_target_price')
            percent: Percentage to apply to reference value (e.g., 5.0 for +5%)
        """
        super().__init__(instrument_name, account, order_recommendation, existing_order, expert_recommendation=None)
        self.take_profit_price = take_profit_price
        self.reference_value = reference_value
        self.percent = percent
    
    def execute(self) -> "TradeActionResult":
        """
        Adjust take profit for existing order using account's set_order_tp method.
        
        Returns:
            TradeActionResult object containing execution results
        """
        try:
            if not self.existing_order:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.ADJUST_TAKE_PROFIT.value,
                    success=False,
                    message="No existing order provided for take profit adjustment",
                    data={}
                )
            
            # Calculate take profit price if not directly provided
            if self.take_profit_price is None:
                if self.reference_value is None or self.percent is None:
                    logger.error(f"No take profit price, reference_value, or percent provided for {self.instrument_name}")
                    return self.create_and_save_action_result(
                        action_type=ExpertActionType.ADJUST_TAKE_PROFIT.value,
                        success=False,
                        message=f"Missing required parameters: take_profit_price or (reference_value + percent)",
                        data={}
                    )
                
                logger.info(f"TP Calculation START for {self.instrument_name} - Order ID: {self.existing_order.id}, Side: {self.existing_order.side.upper()}, reference_value: {self.reference_value}, percent: {self.percent:+.2f}%")
                
                # Get reference price based on reference_value type
                from .types import ReferenceValue
                reference_price = None
                
                if self.reference_value == ReferenceValue.ORDER_OPEN_PRICE.value:
                    # Use the order's limit_price as open price
                    reference_price = self.existing_order.limit_price
                    if reference_price is None:
                        logger.error(f"Order {self.existing_order.id} has no limit_price for ORDER_OPEN_PRICE reference")
                        return self.create_and_save_action_result(
                            action_type=ExpertActionType.ADJUST_TAKE_PROFIT.value,
                            success=False,
                            message="Order has no open price (limit_price) available",
                            data={}
                        )
                    logger.info(f"TP Reference: ORDER_OPEN_PRICE = ${reference_price:.2f} (from order.limit_price)")
                    
                elif self.reference_value == ReferenceValue.CURRENT_PRICE.value:
                    reference_price = self.get_current_price()
                    if reference_price is None:
                        logger.error(f"Cannot get current price for {self.instrument_name}")
                        return self.create_and_save_action_result(
                            action_type=ExpertActionType.ADJUST_TAKE_PROFIT.value,
                            success=False,
                            message=f"Cannot get current market price for {self.instrument_name}",
                            data={}
                        )
                    logger.info(f"TP Reference: CURRENT_PRICE = ${reference_price:.2f} (from market data)")
                    
                elif self.reference_value == ReferenceValue.EXPERT_TARGET_PRICE.value:
                    # Get target price from expert recommendation
                    # Target = price_at_date * (1 + expected_profit_percent/100) for BUY
                    # Target = price_at_date * (1 - expected_profit_percent/100) for SELL
                    if self.existing_order and self.existing_order.expert_recommendation_id:
                        from .db import get_instance
                        from .models import ExpertRecommendation
                        expert_rec = get_instance(ExpertRecommendation, self.existing_order.expert_recommendation_id)
                        if expert_rec and hasattr(expert_rec, 'price_at_date') and hasattr(expert_rec, 'expected_profit_percent'):
                            base_price = expert_rec.price_at_date
                            expected_profit = expert_rec.expected_profit_percent
                            
                            logger.info(f"TP Reference: EXPERT_TARGET_PRICE - base_price: ${base_price:.2f}, expected_profit: {expected_profit:.1f}%, action: {expert_rec.recommended_action}")
                            
                            # Calculate target price based on recommendation direction
                            if expert_rec.recommended_action == OrderRecommendation.BUY:
                                reference_price = base_price * (1 + expected_profit / 100)
                                logger.info(f"TP Target (BUY): ${base_price:.2f} * (1 + {expected_profit:.1f}/100) = ${reference_price:.2f}")
                            elif expert_rec.recommended_action == OrderRecommendation.SELL:
                                reference_price = base_price * (1 - expected_profit / 100)
                                logger.info(f"TP Target (SELL): ${base_price:.2f} * (1 - {expected_profit:.1f}/100) = ${reference_price:.2f}")
                            else:
                                logger.error(f"Invalid recommendation action: {expert_rec.recommended_action}")
                                return self.create_and_save_action_result(
                                    action_type=ExpertActionType.ADJUST_TAKE_PROFIT.value,
                                    success=False,
                                    message=f"Invalid recommendation action: {expert_rec.recommended_action}",
                                    data={}
                                )
                        else:
                            logger.error(f"Cannot get expert target price for order {self.existing_order.id} - missing price_at_date or expected_profit_percent")
                            return self.create_and_save_action_result(
                                action_type=ExpertActionType.ADJUST_TAKE_PROFIT.value,
                                success=False,
                                message="Cannot get expert target price from recommendation",
                                data={}
                            )
                    else:
                        logger.error(f"No expert recommendation linked to order {self.existing_order.id}")
                        return self.create_and_save_action_result(
                            action_type=ExpertActionType.ADJUST_TAKE_PROFIT.value,
                            success=False,
                            message="No expert recommendation available for target price",
                            data={}
                        )
                else:
                    logger.error(f"Unknown reference_value: {self.reference_value}")
                    return self.create_and_save_action_result(
                        action_type=ExpertActionType.ADJUST_TAKE_PROFIT.value,
                        success=False,
                        message=f"Unknown reference_value: {self.reference_value}",
                        data={}
                    )
                
                # Calculate TP price based on reference price and percent
                # For BUY orders: TP is above entry (positive percent)
                # For SELL orders: TP is below entry (negative percent implied)
                if self.existing_order.side.upper() == "BUY":
                    self.take_profit_price = reference_price * (1 + self.percent / 100)
                    logger.info(f"TP Final (BUY): ${reference_price:.2f} * (1 + {self.percent:+.2f}/100) = ${self.take_profit_price:.2f}")
                else:  # SELL order
                    self.take_profit_price = reference_price * (1 - self.percent / 100)
                    logger.info(f"TP Final (SELL): ${reference_price:.2f} * (1 - {self.percent:+.2f}/100) = ${self.take_profit_price:.2f}")
                
                logger.info(f"TP Calculation COMPLETE for {self.instrument_name} - Final TP Price: ${self.take_profit_price:.2f}")
            
            # Use account's set_order_tp method to adjust take profit
            tp_result = self.account.set_order_tp(self.existing_order, self.take_profit_price)
            
            if tp_result is not None:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.ADJUST_TAKE_PROFIT.value,
                    success=True,
                    message=f"Take profit adjusted for {self.instrument_name} to ${self.take_profit_price:.2f}",
                    data={
                        "order_id": self.existing_order.id, 
                        "new_tp_price": self.take_profit_price,
                        "tp_result": str(tp_result)
                    }
                )
            else:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.ADJUST_TAKE_PROFIT.value,
                    success=False,
                    message=f"Failed to adjust take profit for {self.instrument_name}",
                    data={"order_id": self.existing_order.id, "requested_tp_price": self.take_profit_price}
                )
            
        except Exception as e:
            logger.error(f"Error adjusting take profit for {self.instrument_name}: {e}", exc_info=True)
            return self.create_and_save_action_result(
                action_type=ExpertActionType.ADJUST_TAKE_PROFIT.value,
                success=False,
                message=f"Error adjusting take profit: {str(e)}",
                data={"order_id": self.existing_order.id if self.existing_order else None}
            )
    
    def get_description(self) -> str:
        """Get description of adjust take profit action."""
        price_desc = f" at ${self.take_profit_price}" if self.take_profit_price else " (auto-calculated)"
        return f"Set or adjust take profit order for {self.instrument_name}{price_desc}"


class AdjustStopLossAction(TradeAction):
    """Adjust stop loss level for an existing order."""
    
    def __init__(self, instrument_name: str, account: AccountInterface, 
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 stop_loss_price: Optional[float] = None,
                 reference_value: Optional[str] = None, percent: Optional[float] = None):
        """
        Initialize adjust stop loss action.
        
        Args:
            instrument_name: Instrument name
            account: Account interface
            order_recommendation: Order recommendation
            existing_order: Existing order to adjust (required - from enter_market or open position)
            stop_loss_price: New stop loss price (if provided directly)
            reference_value: Reference price type ('order_open_price', 'current_price', 'expert_target_price')
            percent: Percentage to apply to reference value (e.g., -3.0 for -3%)
        """
        super().__init__(instrument_name, account, order_recommendation, existing_order, expert_recommendation=None)
        self.stop_loss_price = stop_loss_price
        self.reference_value = reference_value
        self.percent = percent
    
    def execute(self) -> "TradeActionResult":
        """
        Create or adjust stop loss order linked to existing order.
        
        Returns:
            TradeActionResult object containing execution results
        """
        try:
            if not self.existing_order:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                    success=False,
                    message="No existing order provided for stop loss adjustment",
                    data={}
                )
            
            # Calculate stop loss price if not directly provided
            if self.stop_loss_price is None:
                if self.reference_value is None or self.percent is None:
                    logger.error(f"No stop loss price, reference_value, or percent provided for {self.instrument_name}")
                    return self.create_and_save_action_result(
                        action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                        success=False,
                        message=f"Missing required parameters: stop_loss_price or (reference_value + percent)",
                        data={}
                    )
                
                logger.info(f"SL Calculation START for {self.instrument_name} - Order ID: {self.existing_order.id}, Side: {self.existing_order.side.upper()}, reference_value: {self.reference_value}, percent: {self.percent:+.2f}%")
                
                # Get reference price based on reference_value type
                from .types import ReferenceValue
                reference_price = None
                
                if self.reference_value == ReferenceValue.ORDER_OPEN_PRICE.value:
                    # Use the order's limit_price as open price
                    reference_price = self.existing_order.limit_price
                    if reference_price is None:
                        logger.error(f"Order {self.existing_order.id} has no limit_price for ORDER_OPEN_PRICE reference")
                        return self.create_and_save_action_result(
                            action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                            success=False,
                            message="Order has no open price (limit_price) available",
                            data={}
                        )
                    logger.info(f"SL Reference: ORDER_OPEN_PRICE = ${reference_price:.2f} (from order.limit_price)")
                    
                elif self.reference_value == ReferenceValue.CURRENT_PRICE.value:
                    reference_price = self.get_current_price()
                    if reference_price is None:
                        logger.error(f"Cannot get current price for {self.instrument_name}")
                        return self.create_and_save_action_result(
                            action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                            success=False,
                            message=f"Cannot get current market price for {self.instrument_name}",
                            data={}
                        )
                    logger.info(f"SL Reference: CURRENT_PRICE = ${reference_price:.2f} (from market data)")
                    
                elif self.reference_value == ReferenceValue.EXPERT_TARGET_PRICE.value:
                    # Get target price from expert recommendation
                    # Target = price_at_date * (1 + expected_profit_percent/100) for BUY
                    # Target = price_at_date * (1 - expected_profit_percent/100) for SELL
                    if self.existing_order and self.existing_order.expert_recommendation_id:
                        from .db import get_instance
                        from .models import ExpertRecommendation
                        expert_rec = get_instance(ExpertRecommendation, self.existing_order.expert_recommendation_id)
                        if expert_rec and hasattr(expert_rec, 'price_at_date') and hasattr(expert_rec, 'expected_profit_percent'):
                            base_price = expert_rec.price_at_date
                            expected_profit = expert_rec.expected_profit_percent
                            
                            logger.info(f"SL Reference: EXPERT_TARGET_PRICE - base_price: ${base_price:.2f}, expected_profit: {expected_profit:.1f}%, action: {expert_rec.recommended_action}")
                            
                            # Calculate target price based on recommendation direction
                            if expert_rec.recommended_action == OrderRecommendation.BUY:
                                reference_price = base_price * (1 + expected_profit / 100)
                                logger.info(f"SL Target (BUY): ${base_price:.2f} * (1 + {expected_profit:.1f}/100) = ${reference_price:.2f}")
                            elif expert_rec.recommended_action == OrderRecommendation.SELL:
                                reference_price = base_price * (1 - expected_profit / 100)
                                logger.info(f"SL Target (SELL): ${base_price:.2f} * (1 - {expected_profit:.1f}/100) = ${reference_price:.2f}")
                            else:
                                logger.error(f"Invalid recommendation action: {expert_rec.recommended_action}")
                                return self.create_and_save_action_result(
                                    action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                                    success=False,
                                    message=f"Invalid recommendation action: {expert_rec.recommended_action}",
                                    data={}
                                )
                        else:
                            logger.error(f"Cannot get expert target price for order {self.existing_order.id} - missing price_at_date or expected_profit_percent")
                            return self.create_and_save_action_result(
                                action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                                success=False,
                                message="Cannot get expert target price from recommendation",
                                data={}
                            )
                    else:
                        logger.error(f"No expert recommendation linked to order {self.existing_order.id}")
                        return self.create_and_save_action_result(
                            action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                            success=False,
                            message="No expert recommendation available for target price",
                            data={}
                        )
                else:
                    logger.error(f"Unknown reference_value: {self.reference_value}")
                    return self.create_and_save_action_result(
                        action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                        success=False,
                        message=f"Unknown reference_value: {self.reference_value}",
                        data={}
                    )
                
                # Calculate SL price based on reference price and percent
                # For BUY orders: SL is below entry (negative percent)
                # For SELL orders: SL is above entry (positive percent implied)
                if self.existing_order.side.upper() == "BUY":
                    self.stop_loss_price = reference_price * (1 + self.percent / 100)
                    logger.info(f"SL Final (BUY): ${reference_price:.2f} * (1 + {self.percent:+.2f}/100) = ${self.stop_loss_price:.2f}")
                else:  # SELL order
                    self.stop_loss_price = reference_price * (1 - self.percent / 100)
                    logger.info(f"SL Final (SELL): ${reference_price:.2f} * (1 - {self.percent:+.2f}/100) = ${self.stop_loss_price:.2f}")
                
                logger.info(f"SL Calculation COMPLETE for {self.instrument_name} - Final SL Price: ${self.stop_loss_price:.2f}")
            
            # Determine SL order side (opposite of main order)
            sl_side = "sell" if self.existing_order.side == "buy" else "buy"
            
            # Create stop loss order record
            sl_order_id = self.create_order_record(
                side=sl_side,
                quantity=self.existing_order.quantity,
                order_type="stop",
                stop_price=self.stop_loss_price,
                linked_order_id=self.existing_order.id
            )
            
            if not sl_order_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                    success=False,
                    message="Failed to create stop loss order record",
                    data={}
                )
            
            # Retrieve the order object for submission (needs to be in a session)
            sl_order_record = get_instance(TradingOrder, sl_order_id)
            if not sl_order_record:
                logger.error(f"Failed to retrieve stop loss order {sl_order_id} after creation")
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                    success=False,
                    message="Failed to retrieve stop loss order record",
                    data={}
                )
            
            # Submit SL order through account interface
            submit_result = self.account.submit_order(sl_order_record)
            
            if submit_result is not None:
                # Update SL order record with broker order ID
                if hasattr(submit_result, 'account_order_id') and submit_result.account_order_id:
                    sl_order_record.broker_order_id = str(submit_result.account_order_id)
                    sl_order_record.status = OrderStatus.OPEN.value
                    update_instance(sl_order_record)
                
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                    success=True,
                    message=f"Stop loss order created for {self.instrument_name} at ${self.stop_loss_price}",
                    data={"order_id": sl_order_id, "broker_order_id": submit_result.account_order_id}
                )
            else:
                # Update SL order status to failed
                sl_order_record.status = OrderStatus.CANCELED.value
                update_instance(sl_order_record)
                
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                    success=False,
                    message=f"Failed to submit stop loss order",
                    data={}
                )
                
        except Exception as e:
            logger.error(f"Error executing adjust stop loss action for {self.instrument_name}: {e}", exc_info=True)
            return self.create_and_save_action_result(
                action_type=ExpertActionType.ADJUST_STOP_LOSS.value,
                success=False,
                message=f"Error executing adjust stop loss action: {str(e)}",
                data={}
            )
    
    def get_description(self) -> str:
        """Get description of adjust stop loss action."""
        price_desc = f" at ${self.stop_loss_price}" if self.stop_loss_price else " (auto-calculated)"
        return f"Set or adjust stop loss order for {self.instrument_name}{price_desc}"


# Factory function to create actions based on action type
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
        order_recommendation: Order recommendation
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
    }
    
    action_class = action_map.get(action_type)
    if not action_class:
        raise ValueError(f"Unknown action type: {action_type}")
    
    # Create action with appropriate arguments
    # Only pass expert_recommendation to order-creating actions (BUY, SELL, CLOSE)
    # AdjustTP/SL actions only need existing_order from enter_market or open positions
    if action_type in [ExpertActionType.ADJUST_TAKE_PROFIT, ExpertActionType.ADJUST_STOP_LOSS]:
        return action_class(instrument_name, account, order_recommendation, existing_order, **kwargs)
    else:
        # BUY, SELL, CLOSE actions get expert_recommendation for order linking
        return action_class(instrument_name, account, order_recommendation, existing_order, expert_recommendation)


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