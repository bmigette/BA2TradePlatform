from abc import abstractmethod
from typing import Any, Dict, Optional
from unittest import result
from ..logger import logger
from ..core.models import AccountSetting
from ..core.types import OrderOpenType
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
    def submit_order(self, trading_order, depends_on_order_id: Optional[int] = None, depends_on_status: Optional[str] = None, good_for = "gtc", open_type = OrderOpenType.AUTOMATIC) -> Any:
        """
        Submit a new order to the account.

        Args:
            trading_order: A TradingOrder object containing all order details.
            depends_on_order_id (Optional[int]): ID of the order this order depends on (for conditional/triggered orders).
            depends_on_status (Optional[str]): Status that the depends_on_order must reach to trigger this order.
            good_for (str): Time in force for the order (e.g., "gtc", "day").
            open_type (OrderOpenType): Specifies how the order should be opened (automatic/manual).

        Returns:
            Any: The created order object if successful. Returns None or raises an exception if failed.
        """
        pass

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
