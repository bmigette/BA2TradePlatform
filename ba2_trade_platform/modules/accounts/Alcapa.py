from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from typing import Any, Dict, Optional

from ...logger import logger
from ...core.models import TradingOrder, Position
from ...core.types import OrderStatus
from ...config import APCA_API_KEY_ID, APCA_API_SECRET_KEY, APCA_API_BASE_URL
from ...core.AccountInterface import AccountInterface

class AlpacaAccount(AccountInterface):
    """
    A class that implements the AccountInterface for interacting with Alpaca trading accounts.
    This class provides methods for managing orders, positions, and account information through
    the Alpaca trading API.
    """
    def __init__(self):
        """
        Initialize the AlpacaAccount with API credentials.
        Establishes connection with Alpaca trading API using credentials from config.
        
        Raises:
            Exception: If initialization of Alpaca TradingClient fails.
        """
        try:
            self.client = TradingClient(
                api_key=APCA_API_KEY_ID,
                secret_key=APCA_API_SECRET_KEY,
                paper=True if "paper" in APCA_API_BASE_URL else False,
                #base_url=APCA_API_BASE_URL
            )
            logger.info("Alpaca TradingClient initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize Alpaca TradingClient: {e}")
            raise
    
    def alpaca_order_to_tradingorder(self, order):
        """
        Convert an Alpaca order object to a TradingOrder object.
        
        Args:
            order: An Alpaca order object containing order details.
            
        Returns:
            TradingOrder: A TradingOrder object containing the order information.
        """
        return TradingOrder(
            id=getattr(order, "id", None),
            symbol=getattr(order, "symbol", None),
            quantity=getattr(order, "qty", None),
            side=getattr(order, "side", None),
            order_type=getattr(order, "type", None),
            good_for=getattr(order, "time_in_force", None),
            limit_price=getattr(order, "limit_price", None),
            stop_price=getattr(order, "stop_price", None),
            status=getattr(order, "status", None),
            filled_qty=getattr(order, "filled_qty", None),
            client_order_id=getattr(order, "client_order_id", None),
            created_at=getattr(order, "created_at", None),
        )
    
    def alpaca_position_to_position(self, position):
        """
        Convert an Alpaca position object to a Position object.
        
        Args:
            position: An Alpaca position object containing position details.
            
        Returns:
            Position: A Position object containing the position information.
        """
        return Position(
            symbol=getattr(position, "symbol", None),
            quantity=getattr(position, "qty", None),
            qty_available=getattr(position, "qty_available", None),
            average_entry_price=getattr(position, "avg_entry_price", None),
            avg_entry_swap_rate=getattr(position, "avg_entry_swap_rate", None),
            current_price=getattr(position, "current_price", None),
            lastday_price=getattr(position, "lastday_price", None),
            change_today=getattr(position, "change_today", None),
            unrealized_pl=getattr(position, "unrealized_pl", None),
            unrealized_plpc=getattr(position, "unrealized_plpc", None),
            unrealized_intraday_pl=getattr(position, "unrealized_intraday_pl", None),
            unrealized_intraday_plpc=getattr(position, "unrealized_intraday_plpc", None),
            market_value=getattr(position, "market_value", None),
            cost_basis=getattr(position, "cost_basis", None),
            side=getattr(position, "side", None),
            exchange=getattr(position, "exchange", None),
            asset_class=getattr(position, "asset_class", None),
            swap_rate=getattr(position, "swap_rate", None)
        )
        
    def get_orders(self, status: Optional[OrderStatus] = OrderStatus.ALL): # TODO: Add filter handling
        """
        Retrieve a list of orders based on the provided filter.
        
        Args:
            filter (dict, optional): Filter criteria for orders. Defaults to {}.
            
        Returns:
            list: A list of TradingOrder objects representing the orders.
            Returns empty list if an error occurs.
        """
        try:
            filter = GetOrdersRequest(
                status=status
            )
            alpaca_orders = self.client.get_orders(filter)
            orders = [self.alpaca_order_to_tradingorder(order) for order in alpaca_orders]
            logger.debug(f"Listed {len(orders)} Alpaca.")
            return orders
        except Exception as e:
            logger.error(f"Error listing Alpaca orders: {e}", exc_info=True)
            return []

    def submit_order(self, symbol: str, qty: float, side: str, type: str, time_in_force: str, **kwargs) -> TradingOrder:
        """
        Submit a new order to Alpaca.
        
        Args:
            trading_order (TradingOrder): The order details to submit.
            
        Returns:
            TradingOrder: The created order if successful, None if an error occurs.
        """
        try:
            raise NotImplementedError("Method not implemented yet.")
        except Exception as e:
            logger.error(f"Error creating Alpaca order: {e}")
            return None

    def modify_order(self, order_id: str, trading_order: TradingOrder):
        """
        Modify an existing order in Alpaca.
        
        Args:
            order_id (str): The ID of the order to modify.
            trading_order (TradingOrder): The new order details.
            
        Returns:
            TradingOrder: The modified order if successful, None if an error occurs.
        """
        try:
            order = self.client.replace_order(
                order_id=order_id,
                qty=trading_order.quantity,
                time_in_force=trading_order.time_in_force,
                limit_price=trading_order.limit_price,
                stop_price=trading_order.stop_price
            )
            logger.info(f"Modified Alpaca order: {order.id}")
            return self.alpaca_order_to_tradingorder(order)
        except Exception as e:
            logger.error(f"Error modifying Alpaca order {order_id}: {e}")
            return None

    def get_order(self, order_id: str):
        """
        Retrieve a specific order by its ID.
        
        Args:
            order_id (str): The ID of the order to retrieve.
            
        Returns:
            TradingOrder: The requested order if found, None if an error occurs.
        """
        try:
            order = self.client.get_order_by_id(order_id)
            logger.debug(f"Fetched Alpaca order: {order.id}")
            return self.alpaca_order_to_tradingorder(order)
        except Exception as e:
            logger.error(f"Error fetching Alpaca order {order_id}: {e}")
            return None
        
    def cancel_order(self, order_id: str):
        """
        Cancel an existing order.
        
        Args:
            order_id (str): The ID of the order to cancel.
            
        Returns:
            bool: True if cancellation was successful, False otherwise.
        """
        try:
            self.client.cancel_order(order_id)
            logger.info(f"Cancelled Alpaca order: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Error cancelling Alpaca order {order_id}: {e}")
            return False

    def get_positions(self):
        """
        Retrieve all current positions in the Alpaca account.
        
        Returns:
            list: A list of position objects if successful, empty list if an error occurs.
        """
        try:
            positions = self.client.get_all_positions()
            logger.debug(f"Listed {len(positions)} Alpaca positions.")
            return [self.alpaca_position_to_position(position) for position in positions]
        except Exception as e:
            logger.error(f"Error listing Alpaca positions: {e}")
            return []

    def get_account_info(self):
        """
        Retrieve current account information from Alpaca.
        
        Returns:
            object: Account information if successful, None if an error occurs.
        """
        try:
            account = self.client.get_account()
            logger.debug("Fetched Alpaca account info.")
            return account
        except Exception as e:
            logger.error(f"Error fetching Alpaca account info: {e}")
            return None