from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from unittest import result
from ..logger import logger
from ..core.models import AccountSetting
from ..core.db import get_instance, get_db, update_instance, add_instance
from sqlmodel import select
class AccountInterface(ABC):

    def save_settings(self, settings: Dict[str, Any]):
        """
        Save account settings to the database, converting bool to JSON for storage.
        """
        from ..core.models import AccountSetting
        from sqlmodel import select
        import json
        session = get_db()
        definitions = type(self).get_settings_definitions()
        try:
            for key, value in settings.items():
                definition = definitions.get(key, {})
                value_type = definition.get("type", "str")
                stmt = select(AccountSetting).where(AccountSetting.account_id == self.id, AccountSetting.key == key)
                setting = session.exec(stmt).first()
                if value_type == "json" or value_type == "bool":
                    json_value = json.dumps(value)
                    if setting:
                        setting.value_json = json_value
                        update_instance(setting, session)
                    else:
                        setting = AccountSetting(account_id=self.id, key=key, value_json=json_value)
                        add_instance(setting, session)
                elif value_type == "float":
                    if setting:
                        setting.value_float = float(value)
                        update_instance(setting, session)
                    else:
                        setting = AccountSetting(account_id=self.id, key=key, value_float=float(value))
                        add_instance(setting, session)
                else:
                    if setting:
                        setting.value_str = str(value)
                        update_instance(setting, session)
                    else:
                        setting = AccountSetting(account_id=self.id, key=key, value_str=str(value))
                        add_instance(setting, session)
            session.commit()
            logger.info(f"Saved settings for account ID {self.id}: {settings}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving account settings: {e}")
            raise
        finally:
            session.close()
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


    @classmethod
    @abstractmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """
        Return a dictionary defining the required configuration/settings for the account implementation.

        Returns:
            Dict[str, Any]: A dictionary where keys are setting names and values are metadata such as:
                - type: The expected type (str, float, json)
                - required: Whether the setting is mandatory
                - description: Human-readable description of the setting
        """
        pass

    @property
    def settings(self) -> Dict[str, Any]:
        """
        Loads and returns account settings using the AccountSetting model
        based on the settings definitions provided by the implementation.
        Handles JSON->bool conversion for bool types.
        """
        import json
        try:
            definitions = type(self).get_settings_definitions()
            session = get_db()
            statement = select(AccountSetting).where(AccountSetting.account_id == self.id)
            results = session.exec(statement)
            settings_value_from_db = results.all()
            settings = {k : None for k in definitions.keys()}

            for setting in settings_value_from_db:
                definition = definitions.get(setting.key, {})
                value_type = definition.get("type", "str")
                if value_type == "json":
                    settings[setting.key] = setting.value_json
                elif value_type == "bool":
                    # Convert JSON string to bool
                    try:
                        settings[setting.key] = json.loads(setting.value_json)
                    except Exception:
                        settings[setting.key] = False
                elif value_type == "float":
                    settings[setting.key] = setting.value_float
                else:
                    settings[setting.key] = setting.value_str
            logger.info(f"Loaded settings for account ID {self.id}: {settings}")
            return settings
        except Exception as e:
            logger.error(f"Error loading account settings: {e}")
            raise

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
    def submit_order(self, symbol: str, qty: float, side: str, type: str, time_in_force: str, **kwargs) -> Any:
        """
        Submit a new order to the account.
        
        Args:
            symbol (str): The asset symbol to trade
            qty (float): The quantity to trade
            side (str): Order side ('buy' or 'sell')
            type (str): Order type ('market', 'limit', 'stop', etc.)
            time_in_force (str): Time in force policy ('day', 'gtc', 'ioc', etc.)
            **kwargs: Additional order parameters such as:
                     - limit_price: Price for limit orders
                     - stop_price: Price for stop orders
                     - client_order_id: Custom order identifier
        
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
