from abc import abstractmethod
from typing import Any, Dict, Optional
from unittest import result
from ..logger import logger
from ..core.models import AccountSetting
from ..core.ExtendableSettingsInterface import ExtendableSettingsInterface


class AccountInterface(ExtendableSettingsInterface):
    SETTING_MODEL = AccountSetting
    SETTING_LOOKUP_FIELD = "account_id"
    
    """
    Abstract base class for trading account interfaces.
    Defines the required methods for account implementations.
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
    def submit_order(self, trading_order) -> Any:
        """
        Submit a new order to the account.
        
        Args:
            trading_order: A TradingOrder object containing all order details
        
        Returns:
            Any: The created order object if successful, None or raises exception if failed
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

    def submit_order_with_db_update(self, trading_order):
        """
        Wrapper for submit_order that handles database updates and comment formatting.
        
        Args:
            trading_order: A TradingOrder object containing all order details
            
        Returns:
            TradingOrder: The created order if successful, None if failed
        """
        try:
            from .db import add_instance, update_instance
            from .models import TradingOrder as TradingOrderModel
            from .types import OrderStatus
            
            # Prepend [ORDERID:XX] to comment if not already present
            if trading_order.comment and not trading_order.comment.startswith('[ORDERID:'):
                trading_order.comment = f"[ORDERID:{trading_order.id or 'PENDING'}] {trading_order.comment}"
            
            # Submit the order to the account provider
            submitted_order = self.submit_order(trading_order)
            
            if submitted_order:
                # Update the database record with the submitted order details
                if trading_order.id:
                    # Update existing record
                    db_order = TradingOrderModel.from_orm(submitted_order) if hasattr(TradingOrderModel, 'from_orm') else submitted_order
                    db_order.id = trading_order.id
                    update_instance(db_order)
                    logger.info(f"Updated database order {trading_order.id} with submitted order details")
                else:
                    # Create new database record
                    db_order = TradingOrderModel.from_orm(submitted_order) if hasattr(TradingOrderModel, 'from_orm') else submitted_order
                    order_id = add_instance(db_order)
                    submitted_order.id = order_id
                    logger.info(f"Created new database order {order_id}")
                
                # Update comment with actual order ID if it was pending
                if submitted_order.order_id and '[ORDERID:PENDING]' in submitted_order.comment:
                    submitted_order.comment = submitted_order.comment.replace('[ORDERID:PENDING]', f'[ORDERID:{submitted_order.order_id}]')
                    if trading_order.id:
                        update_instance(submitted_order)
            
            return submitted_order
            
        except Exception as e:
            logger.error(f"Error in submit_order_with_db_update: {e}", exc_info=True)
            return None
    
    def cancel_order_with_db_update(self, order_id: str):
        """
        Wrapper for cancel_order that handles database updates.
        
        Args:
            order_id (str): The unique identifier of the order to cancel
            
        Returns:
            bool: True if cancellation was successful, False otherwise
        """
        try:
            from .db import get_instance, update_instance
            from .models import TradingOrder as TradingOrderModel  
            from .types import OrderStatus
            from sqlmodel import select, Session
            from .db import get_db
            
            # Cancel the order with the account provider
            success = self.cancel_order(order_id)
            
            if success:
                # Update database record to reflect cancellation
                with Session(get_db().bind) as session:
                    # Find the order in the database by order_id
                    statement = select(TradingOrderModel).where(TradingOrderModel.order_id == order_id)
                    db_order = session.exec(statement).first()
                    
                    if db_order:
                        db_order.status = OrderStatus.CANCELED
                        session.add(db_order)
                        session.commit()
                        logger.info(f"Updated database order {db_order.id} status to CANCELED")
                    else:
                        logger.warning(f"Could not find database order with order_id {order_id} to update status")
                        
            return success
            
        except Exception as e:
            logger.error(f"Error in cancel_order_with_db_update: {e}", exc_info=True)
            return False

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
