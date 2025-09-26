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
from .models import TradingOrder, ExpertRecommendation
from .types import OrderRecommendation, ExpertActionType, OrderDirection, OrderType, OrderStatus
from .db import get_db, add_instance, update_instance
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
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None):
        """
        Initialize the trade action.
        
        Args:
            instrument_name: Name of the instrument to trade
            account: Account interface for executing trades
            order_recommendation: The recommendation that triggered this action
            existing_order: Optional existing order related to this action
        """
        self.instrument_name = instrument_name
        self.account = account
        self.order_recommendation = order_recommendation
        self.existing_order = existing_order
        
    @abstractmethod
    def execute(self) -> Dict[str, Any]:
        """
        Execute the trading action.
        
        Returns:
            Dict containing execution results including:
            - success: bool indicating if action was successful
            - message: str with status message
            - data: Any additional data (order ID, etc.)
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
            side: Order side ("buy" or "sell")
            quantity: Order quantity
            order_type: Order type ("market", "limit", "stop", etc.)
            limit_price: Limit price for limit orders
            stop_price: Stop price for stop orders
            linked_order_id: ID of linked order (for TP/SL orders)
            
        Returns:
            TradingOrder instance or None if creation failed
        """
        try:
            order = TradingOrder(
                account_id=self.account.id,
                symbol=self.instrument_name,
                side=side,
                quantity=quantity,
                order_type=order_type,
                limit_price=limit_price,
                stop_price=stop_price,
                status=OrderStatus.PENDING.value,
                linked_order_id=linked_order_id,
                created_at=datetime.now(timezone.utc)
            )
            
            order_id = add_instance(order)
            if order_id:
                order.id = order_id
                return order
            else:
                logger.error("Failed to create order record in database")
                return None
                
        except Exception as e:
            logger.error(f"Error creating order record: {e}", exc_info=True)
            return None


class SellAction(TradeAction):
    """Execute a sell order."""
    
    def execute(self) -> Dict[str, Any]:
        """
        Execute a sell order for the instrument.
        
        Returns:
            Execution result with success status and details
        """
        try:
            # Get current position to determine quantity
            current_position = self.get_current_position()
            if current_position is None or current_position <= 0:
                return {
                    "success": False,
                    "message": f"No long position to sell for {self.instrument_name}",
                    "data": None
                }
            
            # Create order record
            order_record = self.create_order_record(
                side="sell",
                quantity=abs(current_position),  # Sell entire position
                order_type="market"
            )
            
            if not order_record:
                return {
                    "success": False,
                    "message": "Failed to create order record",
                    "data": None
                }
            
            # Submit order through account interface
            result = self.account.submit_order(order_record)
            
            if result.get("success", False):
                # Update order record with broker order ID
                if "order_id" in result:
                    order_record.broker_order_id = str(result["order_id"])
                    order_record.status = OrderStatus.OPEN.value
                    update_instance(order_record)
                
                return {
                    "success": True,
                    "message": f"Sell order submitted for {self.instrument_name}",
                    "data": {"order_id": order_record.id, "broker_order_id": result.get("order_id")}
                }
            else:
                # Update order status to failed
                order_record.status = OrderStatus.CANCELED.value
                update_instance(order_record)
                
                return {
                    "success": False,
                    "message": f"Failed to submit sell order: {result.get('message', 'Unknown error')}",
                    "data": None
                }
                
        except Exception as e:
            logger.error(f"Error executing sell action for {self.instrument_name}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error executing sell action: {str(e)}",
                "data": None
            }


class BuyAction(TradeAction):
    """Execute a buy order."""
    
    def execute(self, quantity: Optional[float] = None) -> Dict[str, Any]:
        """
        Execute a buy order for the instrument.
        
        Args:
            quantity: Optional quantity to buy. If not provided, will calculate based on account balance
            
        Returns:
            Execution result with success status and details
        """
        try:
            # If quantity not provided, calculate based on available buying power
            if quantity is None:
                # TODO: Implement dynamic quantity calculation based on:
                # - Available buying power
                # - Risk management rules
                # - Position sizing strategies
                # For now, use a default small quantity
                quantity = 1.0
            
            current_price = self.get_current_price()
            if current_price is None:
                return {
                    "success": False,
                    "message": f"Cannot get current price for {self.instrument_name}",
                    "data": None
                }
            
            # Create order record
            order_record = self.create_order_record(
                side="buy",
                quantity=quantity,
                order_type="market"
            )
            
            if not order_record:
                return {
                    "success": False,
                    "message": "Failed to create order record",
                    "data": None
                }
            
            # Submit order through account interface
            result = self.account.submit_order(order_record)
            
            if result.get("success", False):
                # Update order record with broker order ID
                if "order_id" in result:
                    order_record.broker_order_id = str(result["order_id"])
                    order_record.status = OrderStatus.OPEN.value
                    update_instance(order_record)
                
                return {
                    "success": True,
                    "message": f"Buy order submitted for {self.instrument_name}",
                    "data": {"order_id": order_record.id, "broker_order_id": result.get("order_id")}
                }
            else:
                # Update order status to failed
                order_record.status = OrderStatus.CANCELED.value
                update_instance(order_record)
                
                return {
                    "success": False,
                    "message": f"Failed to submit buy order: {result.get('message', 'Unknown error')}",
                    "data": None
                }
                
        except Exception as e:
            logger.error(f"Error executing buy action for {self.instrument_name}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error executing buy action: {str(e)}",
                "data": None
            }


class CloseAction(TradeAction):
    """Close existing position (buy to cover short or sell long position)."""
    
    def execute(self) -> Dict[str, Any]:
        """
        Close the existing position for the instrument.
        
        Returns:
            Execution result with success status and details
        """
        try:
            current_position = self.get_current_position()
            if current_position is None or current_position == 0:
                return {
                    "success": False,
                    "message": f"No position to close for {self.instrument_name}",
                    "data": None
                }
            
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
            order_record = self.create_order_record(
                side=side,
                quantity=quantity,
                order_type="market"
            )
            
            if not order_record:
                return {
                    "success": False,
                    "message": "Failed to create order record",
                    "data": None
                }
            
            # Submit order through account interface
            result = self.account.submit_order(order_record)
            
            if result.get("success", False):
                # Update order record with broker order ID
                if "order_id" in result:
                    order_record.broker_order_id = str(result["order_id"])
                    order_record.status = OrderStatus.OPEN.value
                    update_instance(order_record)
                
                return {
                    "success": True,
                    "message": f"Close order submitted for {self.instrument_name}",
                    "data": {"order_id": order_record.id, "broker_order_id": result.get("order_id")}
                }
            else:
                # Update order status to failed
                order_record.status = OrderStatus.CANCELED.value
                update_instance(order_record)
                
                return {
                    "success": False,
                    "message": f"Failed to submit close order: {result.get('message', 'Unknown error')}",
                    "data": None
                }
                
        except Exception as e:
            logger.error(f"Error executing close action for {self.instrument_name}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error executing close action: {str(e)}",
                "data": None
            }


class AdjustTakeProfitAction(TradeAction):
    """Adjust take profit level for an existing order."""
    
    def __init__(self, instrument_name: str, account: AccountInterface, 
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 take_profit_price: Optional[float] = None):
        """
        Initialize adjust take profit action.
        
        Args:
            instrument_name: Instrument name
            account: Account interface
            order_recommendation: Order recommendation
            existing_order: Existing order to adjust (required)
            take_profit_price: New take profit price
        """
        super().__init__(instrument_name, account, order_recommendation, existing_order)
        self.take_profit_price = take_profit_price
    
    def execute(self) -> Dict[str, Any]:
        """
        Create or adjust take profit order linked to existing order.
        
        Returns:
            Execution result with success status and details
        """
        try:
            if not self.existing_order:
                return {
                    "success": False,
                    "message": "No existing order provided for take profit adjustment",
                    "data": None
                }
            
            # If no take profit price provided, calculate based on current price and some percentage
            if self.take_profit_price is None:
                current_price = self.get_current_price()
                if current_price is None:
                    return {
                        "success": False,
                        "message": f"Cannot get current price for {self.instrument_name}",
                        "data": None
                    }
                
                # Default to 5% profit target
                if self.existing_order.side == "buy":
                    self.take_profit_price = current_price * 1.05
                else:
                    self.take_profit_price = current_price * 0.95
            
            # Determine TP order side (opposite of main order)
            tp_side = "sell" if self.existing_order.side == "buy" else "buy"
            
            # Create take profit order record
            tp_order_record = self.create_order_record(
                side=tp_side,
                quantity=self.existing_order.quantity,
                order_type="limit",
                limit_price=self.take_profit_price,
                linked_order_id=self.existing_order.id
            )
            
            if not tp_order_record:
                return {
                    "success": False,
                    "message": "Failed to create take profit order record",
                    "data": None
                }
            
            # Submit TP order through account interface
            result = self.account.submit_order(tp_order_record)
            
            if result.get("success", False):
                # Update TP order record with broker order ID
                if "order_id" in result:
                    tp_order_record.broker_order_id = str(result["order_id"])
                    tp_order_record.status = OrderStatus.OPEN.value
                    update_instance(tp_order_record)
                
                return {
                    "success": True,
                    "message": f"Take profit order created for {self.instrument_name} at ${self.take_profit_price}",
                    "data": {"order_id": tp_order_record.id, "broker_order_id": result.get("order_id")}
                }
            else:
                # Update TP order status to failed
                tp_order_record.status = OrderStatus.CANCELED.value
                update_instance(tp_order_record)
                
                return {
                    "success": False,
                    "message": f"Failed to submit take profit order: {result.get('message', 'Unknown error')}",
                    "data": None
                }
                
        except Exception as e:
            logger.error(f"Error executing adjust take profit action for {self.instrument_name}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error executing adjust take profit action: {str(e)}",
                "data": None
            }


class AdjustStopLossAction(TradeAction):
    """Adjust stop loss level for an existing order."""
    
    def __init__(self, instrument_name: str, account: AccountInterface, 
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 stop_loss_price: Optional[float] = None):
        """
        Initialize adjust stop loss action.
        
        Args:
            instrument_name: Instrument name
            account: Account interface
            order_recommendation: Order recommendation
            existing_order: Existing order to adjust (required)
            stop_loss_price: New stop loss price
        """
        super().__init__(instrument_name, account, order_recommendation, existing_order)
        self.stop_loss_price = stop_loss_price
    
    def execute(self) -> Dict[str, Any]:
        """
        Create or adjust stop loss order linked to existing order.
        
        Returns:
            Execution result with success status and details
        """
        try:
            if not self.existing_order:
                return {
                    "success": False,
                    "message": "No existing order provided for stop loss adjustment",
                    "data": None
                }
            
            # If no stop loss price provided, calculate based on current price and some percentage
            if self.stop_loss_price is None:
                current_price = self.get_current_price()
                if current_price is None:
                    return {
                        "success": False,
                        "message": f"Cannot get current price for {self.instrument_name}",
                        "data": None
                    }
                
                # Default to 3% stop loss
                if self.existing_order.side == "buy":
                    self.stop_loss_price = current_price * 0.97
                else:
                    self.stop_loss_price = current_price * 1.03
            
            # Determine SL order side (opposite of main order)
            sl_side = "sell" if self.existing_order.side == "buy" else "buy"
            
            # Create stop loss order record
            sl_order_record = self.create_order_record(
                side=sl_side,
                quantity=self.existing_order.quantity,
                order_type="stop",
                stop_price=self.stop_loss_price,
                linked_order_id=self.existing_order.id
            )
            
            if not sl_order_record:
                return {
                    "success": False,
                    "message": "Failed to create stop loss order record",
                    "data": None
                }
            
            # Submit SL order through account interface
            result = self.account.submit_order(sl_order_record)
            
            if result.get("success", False):
                # Update SL order record with broker order ID
                if "order_id" in result:
                    sl_order_record.broker_order_id = str(result["order_id"])
                    sl_order_record.status = OrderStatus.OPEN.value
                    update_instance(sl_order_record)
                
                return {
                    "success": True,
                    "message": f"Stop loss order created for {self.instrument_name} at ${self.stop_loss_price}",
                    "data": {"order_id": sl_order_record.id, "broker_order_id": result.get("order_id")}
                }
            else:
                # Update SL order status to failed
                sl_order_record.status = OrderStatus.CANCELED.value
                update_instance(sl_order_record)
                
                return {
                    "success": False,
                    "message": f"Failed to submit stop loss order: {result.get('message', 'Unknown error')}",
                    "data": None
                }
                
        except Exception as e:
            logger.error(f"Error executing adjust stop loss action for {self.instrument_name}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error executing adjust stop loss action: {str(e)}",
                "data": None
            }


# Factory function to create actions based on action type
def create_action(action_type: ExpertActionType, instrument_name: str, account: AccountInterface,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
                 **kwargs) -> TradeAction:
    """
    Factory function to create appropriate action based on action type.
    
    Args:
        action_type: Type of action to create
        instrument_name: Instrument name
        account: Account interface
        order_recommendation: Order recommendation
        existing_order: Optional existing order
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
    if action_type in [ExpertActionType.ADJUST_TAKE_PROFIT, ExpertActionType.ADJUST_STOP_LOSS]:
        return action_class(instrument_name, account, order_recommendation, existing_order, **kwargs)
    else:
        return action_class(instrument_name, account, order_recommendation, existing_order)


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
#    - Trigger next action in sequence when order is fulfilled
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