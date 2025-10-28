"""
Interactive Brokers Account Implementation using ib_async

This module provides the IBKRAccount class for interacting with Interactive Brokers
via the TWS (Trader Workstation) API. It supports true order modification, bracket orders,
and dynamic TP/SL management without the limitations found in other brokers.

Requirements:
- TWS (Trader Workstation) or IB Gateway running locally
- Paper trading account or live account credentials
- API access enabled in TWS/Gateway settings
"""

from ib_async import IB, Stock, Order, LimitOrder, MarketOrder, StopOrder, Trade
from ib_async import Contract, Position as IBPosition, AccountValue
from typing import Any, Dict, Optional, List
from datetime import datetime, timezone, timedelta
import asyncio
from decimal import Decimal

from ...logger import logger
from ...core.models import TradingOrder, Position, Transaction
from ...core.types import OrderDirection, OrderStatus, OrderOpenType, OrderType as CoreOrderType
from ...core.interfaces import AccountInterface
from ...core.db import get_db, get_instance, update_instance, add_instance
from sqlmodel import Session, select


class IBKRAccount(AccountInterface):
    """
    Interactive Brokers account implementation using ib_async.
    
    Provides true order modification capabilities and flexible TP/SL management
    that can be updated on accepted and filled orders without canceling.
    
    Configuration settings required:
    - host: TWS/Gateway host (default: '127.0.0.1')
    - port: TWS port (paper: 7497, live: 7496) or Gateway port (paper: 4002, live: 4001)
    - client_id: Unique client identifier (default: 1)
    - account: Account number (optional for single account)
    - paper_account: Boolean flag for paper trading
    """
    
    def __init__(self, id: int):
        """
        Initialize IBKRAccount with connection to TWS/IB Gateway.
        
        Args:
            id: Account instance ID from database
            
        Raises:
            ValueError: If required settings are missing
            Exception: If connection to TWS/Gateway fails
        """
        super().__init__(id)
        
        self.ib = None
        self._authentication_error = None
        self._connected = False
        
        try:
            # Check required settings
            required_settings = ["host", "port", "client_id"]
            missing_settings = [key for key in required_settings if key not in self.settings]
            
            if missing_settings:
                error_msg = f"Missing required settings: {', '.join(missing_settings)}"
                self._authentication_error = error_msg
                logger.error(f"IBKRAccount {id}: {error_msg}")
                raise ValueError(error_msg)
            
            # Initialize IB connection
            self.ib = IB()
            
            # Connect to TWS/Gateway
            host = self.settings["host"]
            port = int(self.settings["port"])
            client_id = int(self.settings["client_id"])
            
            logger.info(f"Connecting to IBKR TWS/Gateway at {host}:{port} with client_id={client_id}")
            self.ib.connect(host, port, clientId=client_id, readonly=False)
            self._connected = True
            
            # Get account number if not specified
            if "account" not in self.settings or not self.settings["account"]:
                accounts = self.ib.managedAccounts()
                if accounts:
                    self.settings["account"] = accounts[0]
                    logger.info(f"Using IBKR account: {self.settings['account']}")
                else:
                    logger.warning("No managed accounts found")
            
            logger.info(f"IBKR connection established for account {id}")
            
        except Exception as e:
            self._authentication_error = str(e)
            logger.error(f"Failed to connect to IBKR for account {id}: {e}", exc_info=True)
            raise
    
    def __del__(self):
        """Cleanup: Disconnect from IBKR when object is destroyed"""
        if self.ib and self._connected:
            try:
                self.ib.disconnect()
                logger.info(f"IBKR disconnected for account {self.id}")
            except Exception as e:
                logger.error(f"Error disconnecting IBKR: {e}")
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """
        Define required and optional settings for IBKR account.
        
        Returns:
            Dictionary with setting definitions
        """
        return {
            "host": {
                "type": "str",
                "required": True,
                "default": "127.0.0.1",
                "description": "TWS/Gateway host address"
            },
            "port": {
                "type": "int",
                "required": True,
                "default": 7497,
                "description": "TWS/Gateway port (Paper TWS: 7497, Live TWS: 7496, Paper Gateway: 4002, Live Gateway: 4001)"
            },
            "client_id": {
                "type": "int",
                "required": True,
                "default": 1,
                "description": "Unique client ID for this connection"
            },
            "account": {
                "type": "str",
                "required": False,
                "description": "Account number (auto-detected if only one account)"
            },
            "paper_account": {
                "type": "bool",
                "required": True,
                "default": True,
                "description": "Whether this is a paper trading account"
            }
        }
    
    def _ensure_connected(self):
        """Ensure connection to IBKR is active, reconnect if needed"""
        if not self.ib or not self.ib.isConnected():
            logger.warning(f"IBKR connection lost for account {self.id}, reconnecting...")
            try:
                host = self.settings["host"]
                port = int(self.settings["port"])
                client_id = int(self.settings["client_id"])
                self.ib.connect(host, port, clientId=client_id, readonly=False)
                self._connected = True
                logger.info(f"IBKR reconnected for account {self.id}")
            except Exception as e:
                logger.error(f"Failed to reconnect to IBKR: {e}", exc_info=True)
                raise
    
    def _create_contract(self, symbol: str) -> Contract:
        """
        Create an IB contract for a stock symbol.
        
        Args:
            symbol: Stock ticker symbol
            
        Returns:
            IB Contract object
        """
        return Stock(symbol, 'SMART', 'USD')
    
    def _map_order_direction_to_ib(self, direction: OrderDirection) -> str:
        """Map internal OrderDirection to IB action (BUY/SELL)"""
        return "BUY" if direction == OrderDirection.BUY else "SELL"
    
    def _map_ib_action_to_direction(self, action: str) -> OrderDirection:
        """Map IB action to internal OrderDirection"""
        return OrderDirection.BUY if action == "BUY" else OrderDirection.SELL
    
    def _map_ib_status_to_order_status(self, ib_status: str) -> OrderStatus:
        """
        Map IB order status to internal OrderStatus.
        
        IB statuses: PendingSubmit, PreSubmitted, Submitted, Filled, Cancelled, Inactive, ApiCancelled
        """
        status_map = {
            "PendingSubmit": OrderStatus.PENDING,
            "PreSubmitted": OrderStatus.PENDING,
            "Submitted": OrderStatus.ACCEPTED,
            "Filled": OrderStatus.FILLED,
            "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
            "Cancelled": OrderStatus.CANCELLED,
            "Canceled": OrderStatus.CANCELLED,
            "ApiCancelled": OrderStatus.CANCELLED,
            "ApiCanceled": OrderStatus.CANCELLED,
            "Inactive": OrderStatus.CANCELLED,
        }
        return status_map.get(ib_status, OrderStatus.PENDING)
    
    def get_account_info(self) -> Dict[str, Any]:
        """
        Get account information from IBKR.
        
        Returns:
            Dictionary with account details including balance, buying power, etc.
        """
        try:
            self._ensure_connected()
            
            account_number = self.settings.get("account", "")
            
            # Get account values
            account_values = self.ib.accountValues(account_number)
            
            # Extract key values
            result = {
                "account_number": account_number,
                "currency": "USD",
            }
            
            for av in account_values:
                if av.tag == "NetLiquidation":
                    result["equity"] = float(av.value)
                elif av.tag == "TotalCashValue":
                    result["cash"] = float(av.value)
                elif av.tag == "BuyingPower":
                    result["buying_power"] = float(av.value)
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting IBKR account info: {e}", exc_info=True)
            raise
    
    def get_cash_balance(self) -> float:
        """Get available cash balance"""
        try:
            account_info = self.get_account_info()
            return account_info.get("cash", 0.0)
        except Exception as e:
            logger.error(f"Error getting cash balance: {e}", exc_info=True)
            return 0.0
    
    def get_buying_power(self) -> float:
        """Get buying power (available for trading)"""
        try:
            account_info = self.get_account_info()
            return account_info.get("buying_power", 0.0)
        except Exception as e:
            logger.error(f"Error getting buying power: {e}", exc_info=True)
            return 0.0
    
    def get_positions(self, with_orders: bool = False) -> List[Position]:
        """
        Get all positions from IBKR.
        
        Args:
            with_orders: Whether to include related orders
            
        Returns:
            List of Position objects
        """
        try:
            self._ensure_connected()
            
            ib_positions = self.ib.positions()
            positions = []
            
            for ib_pos in ib_positions:
                if ib_pos.account == self.settings.get("account"):
                    # Create Position object
                    position = Position(
                        account_id=self.id,
                        symbol=ib_pos.contract.symbol,
                        quantity=float(ib_pos.position),
                        average_entry_price=float(ib_pos.avgCost) / float(ib_pos.position) if ib_pos.position != 0 else 0,
                        current_price=float(ib_pos.marketPrice) if ib_pos.marketPrice else 0,
                        market_value=float(ib_pos.marketValue) if ib_pos.marketValue else 0,
                        unrealized_pl=float(ib_pos.unrealizedPNL) if ib_pos.unrealizedPNL else 0,
                    )
                    positions.append(position)
            
            return positions
            
        except Exception as e:
            logger.error(f"Error getting IBKR positions: {e}", exc_info=True)
            return []
    
    def submit_order(self, order: TradingOrder) -> TradingOrder:
        """
        Submit an order to IBKR.
        
        Args:
            order: TradingOrder object to submit
            
        Returns:
            Updated TradingOrder with broker_order_id
        """
        try:
            self._ensure_connected()
            
            # Create contract
            contract = self._create_contract(order.symbol)
            
            # Create IB order based on order type
            ib_order = None
            action = self._map_order_direction_to_ib(order.side)
            
            if order.order_type == CoreOrderType.MARKET or order.order_type == CoreOrderType.BUY_MARKET or order.order_type == CoreOrderType.SELL_MARKET:
                ib_order = MarketOrder(action, order.quantity)
            elif order.order_type in [CoreOrderType.LIMIT, CoreOrderType.BUY_LIMIT, CoreOrderType.SELL_LIMIT]:
                ib_order = LimitOrder(action, order.quantity, order.limit_price)
            elif order.order_type in [CoreOrderType.STOP, CoreOrderType.BUY_STOP, CoreOrderType.SELL_STOP]:
                ib_order = StopOrder(action, order.quantity, order.stop_price)
            else:
                raise ValueError(f"Unsupported order type: {order.order_type}")
            
            # Place order
            trade = self.ib.placeOrder(contract, ib_order)
            
            # Wait for order ID to be assigned
            self.ib.sleep(0.1)  # Small delay for order ID assignment
            
            # Update order with broker ID and status
            order.broker_order_id = str(trade.order.orderId)
            order.status = self._map_ib_status_to_order_status(trade.orderStatus.status)
            order.submitted_at = datetime.now(timezone.utc)
            
            update_instance(order)
            
            logger.info(f"Order submitted to IBKR: {order.broker_order_id} for {order.symbol}")
            
            return order
            
        except Exception as e:
            logger.error(f"Error submitting order to IBKR: {e}", exc_info=True)
            order.status = OrderStatus.FAILED
            update_instance(order)
            raise
    
    def cancel_order(self, order: TradingOrder) -> bool:
        """
        Cancel an order at IBKR.
        
        Args:
            order: TradingOrder to cancel
            
        Returns:
            True if cancellation succeeded
        """
        try:
            self._ensure_connected()
            
            if not order.broker_order_id:
                logger.warning(f"Order {order.id} has no broker_order_id, cannot cancel")
                return False
            
            # Find the trade by order ID
            trades = self.ib.trades()
            target_trade = None
            for trade in trades:
                if str(trade.order.orderId) == order.broker_order_id:
                    target_trade = trade
                    break
            
            if target_trade:
                self.ib.cancelOrder(target_trade.order)
                order.status = OrderStatus.CANCELLED
                update_instance(order)
                logger.info(f"Order {order.broker_order_id} cancelled at IBKR")
                return True
            else:
                logger.warning(f"Order {order.broker_order_id} not found at IBKR")
                return False
                
        except Exception as e:
            logger.error(f"Error cancelling order at IBKR: {e}", exc_info=True)
            return False
    
    def modify_order(self, broker_order_id: str, new_order: TradingOrder) -> Optional[TradingOrder]:
        """
        Modify an existing order at IBKR (TRUE in-place modification).
        
        This is the key advantage over Alpaca - IBKR supports true order modification
        without cancel/replace, even on accepted orders.
        
        Args:
            broker_order_id: Broker's order ID
            new_order: TradingOrder with updated parameters
            
        Returns:
            Updated TradingOrder or None if modification failed
        """
        try:
            self._ensure_connected()
            
            # Find the existing trade
            trades = self.ib.trades()
            target_trade = None
            for trade in trades:
                if str(trade.order.orderId) == broker_order_id:
                    target_trade = trade
                    break
            
            if not target_trade:
                logger.error(f"Order {broker_order_id} not found at IBKR")
                return None
            
            # Modify the order in-place
            existing_order = target_trade.order
            
            # Update price parameters
            if new_order.limit_price:
                existing_order.lmtPrice = new_order.limit_price
            if new_order.stop_price:
                existing_order.auxPrice = new_order.stop_price
            if new_order.quantity:
                existing_order.totalQuantity = new_order.quantity
            
            # Place the modified order (IBKR handles this as modification, not new order)
            modified_trade = self.ib.placeOrder(target_trade.contract, existing_order)
            
            # Update database order
            new_order.broker_order_id = broker_order_id  # Same ID!
            new_order.status = self._map_ib_status_to_order_status(modified_trade.orderStatus.status)
            
            logger.info(f"Order {broker_order_id} modified at IBKR (in-place)")
            
            return new_order
            
        except Exception as e:
            logger.error(f"Error modifying order at IBKR: {e}", exc_info=True)
            return None
    
    def _update_broker_tp_order(self, tp_order: TradingOrder, new_tp_price: float) -> None:
        """
        Update TP order at IBKR using true order modification.
        
        Unlike Alpaca, this modifies the existing order in-place without creating a new one.
        """
        try:
            if not tp_order.broker_order_id:
                logger.warning(f"TP order {tp_order.id} has no broker_order_id")
                return
            
            logger.info(f"Modifying IBKR TP order {tp_order.broker_order_id} to ${new_tp_price:.2f}")
            
            # Create temporary order with new price
            temp_order = TradingOrder(
                account_id=tp_order.account_id,
                symbol=tp_order.symbol,
                quantity=tp_order.quantity,
                side=tp_order.side,
                order_type=tp_order.order_type,
                limit_price=new_tp_price,
                status=tp_order.status
            )
            
            # Modify order in-place (same order ID)
            result = self.modify_order(tp_order.broker_order_id, temp_order)
            
            if result:
                # Update database with new price
                tp_order.limit_price = new_tp_price
                update_instance(tp_order)
                logger.info(f"TP order {tp_order.id} modified successfully to ${new_tp_price:.2f}")
            else:
                raise Exception("Order modification returned None")
                
        except Exception as e:
            logger.error(f"Error modifying IBKR TP order: {e}", exc_info=True)
            raise
    
    def _update_broker_sl_order(self, sl_order: TradingOrder, new_sl_price: float) -> None:
        """
        Update SL order at IBKR using true order modification.
        
        Unlike Alpaca, this modifies the existing order in-place without creating a new one.
        """
        try:
            if not sl_order.broker_order_id:
                logger.warning(f"SL order {sl_order.id} has no broker_order_id")
                return
            
            logger.info(f"Modifying IBKR SL order {sl_order.broker_order_id} to ${new_sl_price:.2f}")
            
            # Create temporary order with new price
            temp_order = TradingOrder(
                account_id=sl_order.account_id,
                symbol=sl_order.symbol,
                quantity=sl_order.quantity,
                side=sl_order.side,
                order_type=sl_order.order_type,
                stop_price=new_sl_price,
                status=sl_order.status
            )
            
            # Modify order in-place (same order ID)
            result = self.modify_order(sl_order.broker_order_id, temp_order)
            
            if result:
                # Update database with new price
                sl_order.stop_price = new_sl_price
                update_instance(sl_order)
                logger.info(f"SL order {sl_order.id} modified successfully to ${new_sl_price:.2f}")
            else:
                raise Exception("Order modification returned None")
                
        except Exception as e:
            logger.error(f"Error modifying IBKR SL order: {e}", exc_info=True)
            raise
    
    def refresh_orders(self) -> None:
        """
        Refresh order statuses from IBKR.
        
        Syncs all active orders with current broker state.
        """
        try:
            self._ensure_connected()
            
            # Get all open trades from IBKR
            trades = self.ib.openTrades()
            
            # Get all non-terminal orders from database for this account
            with Session(get_db().bind) as session:
                db_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.account_id == self.id,
                        TradingOrder.broker_order_id.isnot(None),
                        TradingOrder.status.notin_(OrderStatus.get_terminal_statuses())
                    )
                ).all()
                
                # Update each order with current status from IBKR
                for db_order in db_orders:
                    # Find matching trade
                    matching_trade = None
                    for trade in trades:
                        if str(trade.order.orderId) == db_order.broker_order_id:
                            matching_trade = trade
                            break
                    
                    if matching_trade:
                        new_status = self._map_ib_status_to_order_status(matching_trade.orderStatus.status)
                        if new_status != db_order.status:
                            logger.info(f"Order {db_order.broker_order_id} status changed: {db_order.status.value} -> {new_status.value}")
                            db_order.status = new_status
                            
                            # Update filled quantity and average price if filled
                            if new_status in [OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]:
                                db_order.filled_quantity = float(matching_trade.orderStatus.filled)
                                if matching_trade.orderStatus.avgFillPrice:
                                    db_order.open_price = float(matching_trade.orderStatus.avgFillPrice)
                                if new_status == OrderStatus.FILLED:
                                    db_order.filled_at = datetime.now(timezone.utc)
                            
                            session.add(db_order)
                
                session.commit()
                
        except Exception as e:
            logger.error(f"Error refreshing IBKR orders: {e}", exc_info=True)
    
    def get_instrument_current_price(self, symbol: str) -> Optional[float]:
        """
        Get current market price for an instrument.
        
        Args:
            symbol: Stock ticker symbol
            
        Returns:
            Current price or None if not available
        """
        try:
            self._ensure_connected()
            
            contract = self._create_contract(symbol)
            
            # Request market data
            ticker = self.ib.reqMktData(contract)
            self.ib.sleep(0.5)  # Wait for data
            
            # Get last price
            if ticker.last and ticker.last > 0:
                return float(ticker.last)
            elif ticker.close and ticker.close > 0:
                return float(ticker.close)
            
            logger.warning(f"No price data available for {symbol}")
            return None
            
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}", exc_info=True)
            return None
