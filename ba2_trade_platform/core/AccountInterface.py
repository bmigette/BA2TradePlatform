from abc import abstractmethod
from typing import Any, Dict, Optional
from unittest import result
from ..logger import logger
from ..core.models import AccountSetting
from ..core.types import OrderOpenType, OrderDirection, OrderType, OrderStatus
from ..core.ExtendableSettingsInterface import ExtendableSettingsInterface


class AccountInterface(ExtendableSettingsInterface):
    SETTING_MODEL = AccountSetting
    SETTING_LOOKUP_FIELD = "account_id"
    
    """
    Abstract base class for trading account interfaces.
    
    This class defines the required interface for all account provider implementations in the BA2 Trade Platform.
    Subclasses must implement all abstract methods to support account info retrieval, order management, position tracking,
    and broker synchronization. All account plugins should inherit from this class and provide concrete implementations
    for the required methods.
    """
    def __init__(self, id: int):
        """
        Initialize the account with a unique identifier.

        Args:
            id (int): The unique identifier for the account.
        """
        self.id = id




    @abstractmethod
    def get_account_info(self) -> Dict[str, Any]:
        """
        Retrieve account information such as balance, equity, buying power, etc.
        
        Returns:
            Dict[str, Any]: A dictionary containing account information with fields such as:
                - balance: Current account balance
                - equity: Total account value including positions
                - buying_power: Available funds for trading
                - etc.
        """
        pass

    @abstractmethod
    def get_positions(self) -> Any:
        """
        Retrieve all current open positions in the account.
        
        Returns:
            Any: A list or collection of position objects containing information such as:
                - symbol: The asset symbol
                - quantity: Position size
                - average_price: Average entry price
                - current_price: Current market price
                - unrealized_pl: Unrealized profit/loss
        """
        pass

    @abstractmethod
    def get_orders(self, status: Optional[str] = None) -> Any:
        """
        Retrieve orders, optionally filtered by status.
        
        Args:
            status (Optional[str]): The status to filter orders by (e.g., 'open', 'closed', 'canceled').
                                  If None, returns all orders.
        
        Returns:
            Any: A list or collection of order objects containing information such as:
                - id: Order identifier
                - symbol: The asset symbol
                - quantity: Order size
                - side: Buy/Sell
                - type: Market/Limit/Stop
                - status: Current order status
                - filled_quantity: Amount filled
                - created_at: Order creation timestamp
        """
        pass

    @abstractmethod
    def _submit_order_impl(self, trading_order) -> Any:
        """
        Internal implementation of order submission. This method should be implemented by child classes.
        The public submit_order method will call this after validation.

        Args:
            trading_order: A validated TradingOrder object containing all order details.

        Returns:
            Any: The created order object if successful. Returns None or raises an exception if failed.
        """
        pass

    def submit_order(self, trading_order) -> Any:
        """
        Submit a new order to the account with validation.

        Args:
            trading_order: A TradingOrder object containing all order details.

        Returns:
            Any: The created order object if successful. Returns None or raises an exception if failed.
        """
        # Validate the trading order before submission
        validation_result = self._validate_trading_order(trading_order)
        if not validation_result['is_valid']:
            error_msg = f"Order validation failed: {', '.join(validation_result['errors'])}"
            logger.error(f"Order validation failed for order: {error_msg}", exc_info=True)
            raise ValueError(error_msg)
        
        # Log successful validation
        logger.info(f"Order validation passed for {trading_order.symbol} - {trading_order.side.value} {trading_order.quantity} @ {trading_order.order_type.value}")
        trading_order.account_id = self.id  # Ensure the order's account_id matches this account
        # Call the child class implementation
        return self._submit_order_impl(trading_order)

    def _validate_trading_order(self, trading_order) -> Dict[str, Any]:
        """
        Validate a trading order before submission.
        
        Args:
            trading_order: The TradingOrder object to validate
            
        Returns:
            Dict[str, Any]: Validation result with 'is_valid' (bool) and 'errors' (list) keys
        """
        errors = []
        
        # Check if trading_order exists
        if trading_order is None:
            errors.append("trading_order cannot be None")
            return {'is_valid': False, 'errors': errors}
        
        # Validate required fields
        if not hasattr(trading_order, 'symbol') or not trading_order.symbol:
            errors.append("symbol is required and cannot be empty")
        elif not isinstance(trading_order.symbol, str):
            errors.append("symbol must be a string")
        elif len(trading_order.symbol.strip()) == 0:
            errors.append("symbol cannot be empty or whitespace only")
            
        if not hasattr(trading_order, 'quantity') or trading_order.quantity is None:
            errors.append("quantity is required")
        elif not isinstance(trading_order.quantity, (int, float)):
            errors.append("quantity must be a number")
        elif trading_order.quantity <= 0:
            errors.append("quantity must be greater than 0")
            
        if not hasattr(trading_order, 'side') or trading_order.side is None:
            errors.append("side is required")
        elif not isinstance(trading_order.side, OrderDirection):
            errors.append(f"side must be an OrderDirection enum, got {type(trading_order.side)}")
            
        if not hasattr(trading_order, 'order_type') or trading_order.order_type is None:
            errors.append("order_type is required")
        elif not isinstance(trading_order.order_type, OrderType):
            errors.append(f"order_type must be an OrderType enum, got {type(trading_order.order_type)}")
            
        if not hasattr(trading_order, 'account_id') or trading_order.account_id is None:
            errors.append("account_id is required")
        elif not isinstance(trading_order.account_id, int):
            errors.append("account_id must be an integer")
        elif trading_order.account_id != self.id:
            errors.append(f"order account_id ({trading_order.account_id}) does not match this account ({self.id})")
            
        # Validate limit orders have limit_price
        if (hasattr(trading_order, 'order_type') and 
            trading_order.order_type in [OrderType.BUY_LIMIT, OrderType.SELL_LIMIT]):
            if not hasattr(trading_order, 'limit_price') or trading_order.limit_price is None:
                errors.append(f"limit_price is required for {trading_order.order_type.value} orders")
            elif not isinstance(trading_order.limit_price, (int, float)):
                errors.append("limit_price must be a number")
            elif trading_order.limit_price <= 0:
                errors.append("limit_price must be greater than 0")
                
        # Validate stop orders have stop_price
        if (hasattr(trading_order, 'order_type') and 
            trading_order.order_type in [OrderType.BUY_STOP, OrderType.SELL_STOP]):
            if not hasattr(trading_order, 'stop_price') or trading_order.stop_price is None:
                errors.append(f"stop_price is required for {trading_order.order_type.value} orders")
            elif not isinstance(trading_order.stop_price, (int, float)):
                errors.append("stop_price must be a number")
            elif trading_order.stop_price <= 0:
                errors.append("stop_price must be greater than 0")
                
        # Validate status if present
        if hasattr(trading_order, 'status') and trading_order.status is not None:
            if not isinstance(trading_order.status, OrderStatus):
                errors.append(f"status must be an OrderStatus enum, got {type(trading_order.status)}")
                
        # Validate open_type if present
        if hasattr(trading_order, 'open_type') and trading_order.open_type is not None:
            if not isinstance(trading_order.open_type, OrderOpenType):
                errors.append(f"open_type must be an OrderOpenType enum, got {type(trading_order.open_type)}")
                
        # Validate dependency fields
        if (hasattr(trading_order, 'depends_on_order') and trading_order.depends_on_order is not None):
            if not isinstance(trading_order.depends_on_order, int):
                errors.append("depends_on_order must be an integer")
            elif trading_order.depends_on_order <= 0:
                errors.append("depends_on_order must be a positive integer")
                
            # If depends_on_order is set, depends_order_status_trigger should also be set
            if (not hasattr(trading_order, 'depends_order_status_trigger') or 
                trading_order.depends_order_status_trigger is None):
                errors.append("depends_order_status_trigger is required when depends_on_order is set")
            elif not isinstance(trading_order.depends_order_status_trigger, OrderStatus):
                errors.append("depends_order_status_trigger must be an OrderStatus enum")
                
        # Validate string fields for length and content
        if hasattr(trading_order, 'comment') and trading_order.comment is not None:
            if not isinstance(trading_order.comment, str):
                errors.append("comment must be a string")
            elif len(trading_order.comment) > 1000:  # Reasonable limit
                errors.append("comment is too long (max 1000 characters)")
                
        if hasattr(trading_order, 'good_for') and trading_order.good_for is not None:
            if not isinstance(trading_order.good_for, str):
                errors.append("good_for must be a string")
            elif trading_order.good_for.lower() not in ['gtc', 'day', 'ioc', 'fok']:
                errors.append("good_for must be one of: 'gtc', 'day', 'ioc', 'fok'")
                
        return {
            'is_valid': len(errors) == 0,
            'errors': errors
        }

    @abstractmethod
    def cancel_order(self, order_id: str) -> Any:
        """
        Cancel an existing order by order ID.
        
        Args:
            order_id (str): The unique identifier of the order to cancel
        
        Returns:
            Any: True if cancellation was successful, False or raises exception if failed
        """
        pass
    
    @abstractmethod
    def modify_order(self, order_id: str) -> Any:
        """
        Modify an existing order by order ID.
        
        Args:
            order_id (str): The unique identifier of the order to cancel
        
        Returns:
            Any: True if modification was successful, False or raises exception if failed
        """
        pass

    @abstractmethod
    def get_order(self, order_id: str) -> Any:
        """
        Retrieve a specific order by order ID.
        
        Args:
            order_id (str): The unique identifier of the order to retrieve
        
        Returns:
            Any: The order object if found, None or raises exception if not found
                Order object typically contains:
                - id: Order identifier
                - symbol: The asset symbol
                - quantity: Order size
                - status: Current order status
                - filled_quantity: Amount filled
                - created_at: Order creation timestamp
        """
        pass


    @abstractmethod
    def get_instrument_current_price(self, symbol: str) -> Optional[float]:
        """
        Get the current market price for a given instrument/symbol.
        
        Args:
            symbol (str): The asset symbol to get the price for
        
        Returns:
            Optional[float]: The current price if available, None if not found or error occurred
        """
        pass

    @abstractmethod
    def refresh_positions(self) -> bool:
        """
        Refresh/synchronize account positions from the broker.
        This method should update any cached position data with fresh data from the broker.
        
        Returns:
            bool: True if refresh was successful, False otherwise
        """
        pass

    @abstractmethod
    def refresh_orders(self) -> bool:
        """
        Refresh/synchronize account orders from the broker.
        This method should update any cached order data and sync database records
        with the current state of orders at the broker.
        
        Returns:
            bool: True if refresh was successful, False otherwise
        """
        pass
