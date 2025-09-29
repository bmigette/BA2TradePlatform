from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, LimitOrderRequest, StopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from typing import Any, Dict, Optional

from ...logger import logger
from ...core.models import TradingOrder, Position
from ...core.types import OrderDirection, OrderStatus

from ...core.AccountInterface import AccountInterface

class AlpacaAccount(AccountInterface):
    """
    A class that implements the AccountInterface for interacting with Alpaca trading accounts.
    This class provides methods for managing orders, positions, and account information through
    the Alpaca trading API.
    """
    def __init__(self, id: int):
        """
        Initialize the AlpacaAccount with API credentials.
        Establishes connection with Alpaca trading API using credentials from config.

        Args:
            id (int): The unique identifier for the account.
        
        Raises:
            Exception: If initialization of Alpaca TradingClient fails.
        """
        super().__init__(id)

        try:
         
            self.client = TradingClient(
                api_key=self.settings["api_key"],
                secret_key=self.settings["api_secret"],
                paper=self.settings["paper_account"], # True if "paper" in APCA_API_BASE_URL else False
            )
            logger.info("Alpaca TradingClient initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize Alpaca TradingClient: {e}", exc_info=True)
            raise
        
    def get_settings_definitions() -> Dict[str, Any]:
        """
        Return the settings definitions required for AlpacaAccount.

        Returns:
            dict: Dictionary with setting names and their types.
        """
        return {
            "api_key": {"type": 'str', "required": True, "description": "Alpaca API Key ID"},
            "api_secret": {"type": 'str', "required": True, "description": "Alpaca API Secret Key"},
            "paper_account": {"type": 'bool', "required": True, "description": "Is this a paper trading account?"}
        }
    def alpaca_order_to_tradingorder(self, order):
        """
        Convert an Alpaca order object to a TradingOrder object.
        
        Args:
            order: An Alpaca order object containing order details.
            
        Returns:
            TradingOrder: A TradingOrder object containing the order information.
        """
        return TradingOrder(
            order_id=getattr(order, "id", None),
            broker_order_id=str(getattr(order, "id", None)) if getattr(order, "id", None) else None,  # Set Alpaca order ID as broker_order_id
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
            qty=float(getattr(position, "qty")) if getattr(position, "qty") is not None else None,
            qty_available=float(getattr(position, "qty_available")) if getattr(position, "qty_available") is not None else None,
            avg_entry_price=float(getattr(position, "avg_entry_price")) if getattr(position, "avg_entry_price") is not None else None,
            avg_entry_swap_rate=float(getattr(position, "avg_entry_swap_rate")) if getattr(position, "avg_entry_swap_rate") is not None else None,
            current_price=float(getattr(position, "current_price")) if getattr(position, "current_price") is not None else None,
            lastday_price=float(getattr(position, "lastday_price")) if getattr(position, "lastday_price") is not None else None,
            change_today=float(getattr(position, "change_today")) if getattr(position, "change_today") is not None else None,
            unrealized_pl=float(getattr(position, "unrealized_pl")) if getattr(position, "unrealized_pl") is not None else None,
            unrealized_plpc=float(getattr(position, "unrealized_plpc")) if getattr(position, "unrealized_plpc") is not None else None,
            unrealized_intraday_pl=float(getattr(position, "unrealized_intraday_pl")) if getattr(position, "unrealized_intraday_pl") is not None else None,
            unrealized_intraday_plpc=float(getattr(position, "unrealized_intraday_plpc")) if getattr(position, "unrealized_intraday_plpc") is not None else None,
            market_value=float(getattr(position, "market_value")) if getattr(position, "market_value") is not None else None,
            cost_basis=float(getattr(position, "cost_basis")) if getattr(position, "cost_basis") is not None else None,
            side=OrderDirection.BUY if getattr(position, "side") == "long" or getattr(position, "side") == "buy" else OrderDirection.SELL,
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
            logger.debug(f"Listed {len(orders)} Alpaca Orders.")
            return orders
        except Exception as e:
            logger.error(f"Error listing Alpaca orders: {e}", exc_info=True)
            return []

    def submit_order(self, trading_order) -> TradingOrder:
        """
        Submit a new order to Alpaca.
        
        Args:
            trading_order: A TradingOrder object containing all order details
            
        Returns:
            TradingOrder: The created order if successful, None if an error occurs.
        """
        try:
            # Convert string values to enums
            side = OrderSide.BUY if trading_order.side.upper() == 'BUY' else OrderSide.SELL
            time_in_force = TimeInForce.DAY if trading_order.good_for == 'day' else TimeInForce.GTC
            
            # Create the appropriate order request based on order type
            if trading_order.order_type.lower() == 'market':
                order_request = MarketOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    client_order_id=trading_order.comment
                )
            elif trading_order.order_type.lower() == 'limit':
                order_request = LimitOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    limit_price=trading_order.limit_price,
                    client_order_id=trading_order.comment
                )
            elif trading_order.order_type.lower() == 'stop':
                order_request = StopOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    stop_price=trading_order.stop_price,
                    client_order_id=trading_order.comment
                )
            else:
                logger.error(f"Unsupported order type: {trading_order.order_type}")
                return None
            
            order = self.client.submit_order(order_request)
            logger.info(f"Submitted Alpaca order: {order.id}")
            
            # Convert to TradingOrder and set the broker_order_id
            result_order = self.alpaca_order_to_tradingorder(order)
            result_order.broker_order_id = str(order.id)  # Set the Alpaca order ID
            
            return result_order
        except Exception as e:
            logger.error(f"Error creating Alpaca order: {e}", exc_info=True)
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
            logger.error(f"Error modifying Alpaca order {order_id}: {e}", exc_info=True)
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
            logger.error(f"Error fetching Alpaca order {order_id}: {e}", exc_info=True)
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
            logger.error(f"Error cancelling Alpaca order {order_id}: {e}", exc_info=True)
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
            logger.error(f"Error listing Alpaca positions: {e}", exc_info=True)
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
            logger.error(f"Error fetching Alpaca account info: {e}", exc_info=True)
            return None

    def get_instrument_current_price(self, symbol: str) -> Optional[float]:
        """
        Get the current market price for a given instrument/symbol.
        
        Args:
            symbol (str): The asset symbol to get the price for
        
        Returns:
            Optional[float]: The current price if available, None if not found or error occurred
        """
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestQuoteRequest
            
            # Create data client for market data
            data_client = StockHistoricalDataClient(
                api_key=self.settings["api_key"],
                secret_key=self.settings["api_secret"]
            )
            
            # Get latest quote
            request = StockLatestQuoteRequest(symbol_or_symbols=[symbol])
            quotes = data_client.get_stock_latest_quote(request)
            
            if symbol in quotes:
                quote = quotes[symbol]
                # Use bid-ask midpoint as current price
                if quote.bid_price and quote.ask_price:
                    current_price = (float(quote.bid_price) + float(quote.ask_price)) / 2
                elif quote.bid_price:
                    current_price = float(quote.bid_price)
                elif quote.ask_price:
                    current_price = float(quote.ask_price)
                else:
                    current_price = None
                
                logger.debug(f"Current price for {symbol}: {current_price}")
                return current_price
            else:
                logger.warning(f"No quote data found for symbol {symbol}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting current price for {symbol}: {e}", exc_info=True)
            return None
        
        
    def refresh_positions(self) -> bool:
        """
        Refresh/synchronize account positions from Alpaca broker.
        This method updates any cached position data with fresh data from the broker.
        
        Returns:
            bool: True if refresh was successful, False otherwise
        """
        try:
            positions = self.get_positions()
            logger.info(f"Successfully refreshed {len(positions)} positions from Alpaca")
            return True
        except Exception as e:
            logger.error(f"Error refreshing positions from Alpaca: {e}", exc_info=True)
            return False

    def refresh_orders(self) -> bool:
        """
        Refresh/synchronize account orders from Alpaca broker.
        This method updates database records with current order states from the broker.
        
        Returns:
            bool: True if refresh was successful, False otherwise
        """
        try:
            from ...core.db import get_db, update_instance
            from ...core.models import TradingOrder as TradingOrderModel
            from sqlmodel import Session, select
            
            # Get all orders from Alpaca
            alpaca_orders = self.get_orders(OrderStatus.ALL)
            
            if not alpaca_orders:
                logger.warning("No orders returned from Alpaca during refresh")
                return True
            
            updated_count = 0
            
            # Update database records with current Alpaca order states
            with Session(get_db().bind) as session:
                for alpaca_order in alpaca_orders:
                    if not alpaca_order.order_id:
                        continue
                        
                    # Find corresponding database record
                    # Convert UUID to string for SQLite compatibility
                    order_id_str = str(alpaca_order.order_id)
                    statement = select(TradingOrderModel).where(
                        TradingOrderModel.broker_order_id == order_id_str
                    )
                    db_order = session.exec(statement).first()
                    
                    if db_order:
                        # Update database order with current Alpaca state
                        db_order.status = alpaca_order.status
                        db_order.filled_qty = alpaca_order.filled_qty
                        
                        # Update broker_order_id if it wasn't set before
                        if not db_order.broker_order_id:
                            db_order.broker_order_id = str(alpaca_order.order_id)
                        
                        session.add(db_order)
                        updated_count += 1
                        logger.debug(f"Updated database order {db_order.id} with Alpaca order {alpaca_order.order_id}")
                
                session.commit()
            
            logger.info(f"Successfully refreshed orders from Alpaca: {updated_count} database records updated")
            return True
            
        except Exception as e:
            logger.error(f"Error refreshing orders from Alpaca: {e}", exc_info=True)
            return False