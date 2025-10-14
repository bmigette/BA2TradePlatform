from abc import abstractmethod
from typing import Any, Dict, List, Optional
from unittest import result
from sqlmodel import Session, select
from ...logger import logger
from ...core.models import ExpertSetting, MarketAnalysis, Transaction, ExpertInstance
from ...core.types import TransactionStatus, OrderDirection
from ...core.db import get_instance, get_db
from .ExtendableSettingsInterface import ExtendableSettingsInterface
from .AccountInterface import AccountInterface


class MarketExpertInterface(ExtendableSettingsInterface):
    SETTING_MODEL = ExpertSetting
    SETTING_LOOKUP_FIELD = "instance_id"
    
    """
    Abstract base class for trading account interfaces.
    Defines the required methods for account implementations.
    """
    def __init__(self, id: int):
        """
        Initialize the account with a unique identifier.

        Args:
            id (int): The unique identifier for the Expert Instance.
        """
        self.id = id
        # Initialize settings cache to None (will be loaded on first access)
        self._settings_cache = None
        
        # Ensure builtin settings are initialized
        self._ensure_builtin_settings()
    
    @property
    def shortname(self) -> str:
        """
        Return a short identifier combining class name and instance ID.
        Used as order comment to identify which expert created the order.
        
        Returns:
            str: Short name in format "classname-instanceid" (e.g., "tradingagents-1")
        """
        class_name = self.__class__.__name__.lower()
        return f"{class_name}-{self.id}"
    
    @classmethod
    def _ensure_builtin_settings(cls):
        """Ensure builtin settings are initialized for the class."""
        if not cls._builtin_settings:
            cls._builtin_settings = {
                # Trading Permissions (generic settings for all market experts)
                "enable_buy": {
                    "type": "bool", "required": False, "default": True,
                    "description": "Allow buy orders for this expert"
                },
                "enable_sell": {
                    "type": "bool", "required": False, "default": False,
                    "description": "Allow sell orders for this expert"
                },
                "allow_automated_trade_opening": {
                    "type": "bool", "required": False, "default": False,
                    "description": "Allow automatic opening of new trading positions"
                },
                "allow_automated_trade_modification": {
                    "type": "bool", "required": False, "default": False,
                    "description": "Allow automatic modification and closing of existing positions"
                },
                # Execution Schedule Settings
                "execution_schedule_enter_market": {
                    "type": "json", "required": False, "default": {
                        "days": {"monday": True, "tuesday": True, "wednesday": True, "thursday": True, "friday": True, "saturday": False, "sunday": False},
                        "times": ["09:30"]
                    },
                    "description": "Schedule configuration for entering new market positions"
                },
                "execution_schedule_open_positions": {
                    "type": "json", "required": False, "default": {
                        "days": {"monday": True, "tuesday": True, "wednesday": True, "thursday": True, "friday": True, "saturday": False, "sunday": False},
                        "times": ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
                    },
                    "description": "Schedule configuration for managing existing open positions"
                },
                "enabled_instruments": {
                    "type": "json", "required": False, "default": {},
                    "description": "Configuration of enabled instruments for this expert instance"
                },
                "instrument_selection_method": {
                    "type": "str", "required": False, "default": "static",
                    "description": "Method for selecting instruments: static (manual), dynamic (AI prompt), expert (expert-driven)",
                    "choices": ["static", "dynamic", "expert"]
                },
                # Balance Management Settings
                "min_available_balance_pct": {
                    "type": "float", "required": False, "default": 20.0,
                    "description": "Minimum available balance percentage required to enter new market positions (default 20%)"
                },
                # Risk Management Settings
                "max_virtual_equity_per_instrument_percent": {
                    "type": "float", "required": False, "default": 10.0,
                    "description": "Maximum virtual equity allocation per instrument (%)",
                    "tooltip": "Maximum percentage of virtual trading balance that can be allocated to a single instrument. This helps maintain portfolio diversification. Recommended: 5-15%. Lower values (5-10%) provide better diversification, higher values (10-15%) allow larger positions in high-confidence trades."
                }
                
            }

    @classmethod
    @abstractmethod
    def description(cls) -> str:
        """
        Abstract property for a human-readable description of the expert.
        Returns:
            str: Description of the expert instance.
        """
        pass

    @classmethod
    def get_expert_properties(cls) -> Dict[str, Any]:
        """
        Get expert-specific properties and capabilities.
        
        Returns:
            Dict[str, Any]: Dictionary containing expert properties and capabilities
        """
        return {
            "can_recommend_instruments": False,  # Default: expert cannot recommend its own instruments
        }
    
    @abstractmethod
    def render_market_analysis(self, market_analysis: MarketAnalysis) -> str:
        """
        Render a human-readable summary of the market analysis results.

        Args:
            market_analysis (MarketAnalysis): The market analysis instance to render.
        """
        pass 
    
    def set_enabled_instruments(self, instrument_configs: Dict[str, Dict]):
        """
        Set the enabled instruments and their configuration.
        
        Args:
            instrument_configs: Dict mapping instrument symbol to config dict
                                containing 'enabled' and 'weight' keys
        """
        logger.debug(f'Setting enabled instruments: {list(instrument_configs.keys())}')

        # Filter to only enabled instruments
        enabled_configs = {
            symbol: config for symbol, config in instrument_configs.items()
            if config.get('enabled', False)
        }

        # Save to expert settings
        self.save_setting('enabled_instruments', enabled_configs)

    def get_enabled_instruments(self) -> List[str]:
        """
        Get a list of all enabled instruments for this expert instance.
        
        This method handles three instrument selection methods:
        - 'static': Returns instruments from enabled_instruments setting
        - 'dynamic': Returns ['DYNAMIC'] to indicate AI-powered selection
        - 'expert': Returns ['EXPERT'] to indicate expert-driven selection
        
        Returns:
            List[str]: List of enabled instrument symbols/identifiers, or special symbols ['EXPERT'] or ['DYNAMIC']
        """
        #logger.debug('Getting enabled instruments from settings')
        
        try:
            # Check instrument selection method
            instrument_selection_method = self.settings.get('instrument_selection_method', 'static')
            
            # Get expert properties to check capabilities
            expert_properties = self.__class__.get_expert_properties()
            can_recommend_instruments = expert_properties.get('can_recommend_instruments', False)
            
            # Handle special instrument selection methods
            if instrument_selection_method == 'expert' and can_recommend_instruments:
                # Expert-driven selection - return EXPERT symbol
                return ["EXPERT"]
            elif instrument_selection_method == 'dynamic':
                # Dynamic AI selection - return DYNAMIC symbol
                return ["DYNAMIC"]
            
            # Static method (default) - get from enabled_instruments setting
            enabled_instruments_setting = self.settings.get('enabled_instruments')
            
            if enabled_instruments_setting:
                # If it's already a dict, return the keys
                if isinstance(enabled_instruments_setting, dict):
                    enabled_instruments = list(enabled_instruments_setting.keys())
                # If it's a string, try to parse it as JSON
                elif isinstance(enabled_instruments_setting, str):
                    try:
                        import json
                        parsed_config = json.loads(enabled_instruments_setting)
                        enabled_instruments = list(parsed_config.keys()) if isinstance(parsed_config, dict) else []
                    except (json.JSONDecodeError, ValueError):
                        logger.warning(f"Failed to parse enabled_instruments setting as JSON: {enabled_instruments_setting}")
                        enabled_instruments = []
                # If it's a list, return it directly
                elif isinstance(enabled_instruments_setting, list):
                    enabled_instruments = enabled_instruments_setting
                else:
                    logger.warning(f"Unexpected type for enabled_instruments setting: {type(enabled_instruments_setting)}")
                    enabled_instruments = []
            else:
                # Return empty list if no enabled instruments configured
                enabled_instruments = []
            
            #logger.debug(f'Found {len(enabled_instruments)} enabled instruments: {enabled_instruments}')
            return enabled_instruments
            
        except Exception as e:
            logger.error(f'Error getting enabled instruments: {e}', exc_info=True)
            return []
    
    def _get_enabled_instruments_config(self) -> Dict[str, Dict]:
        """
        Get the configuration of enabled instruments from settings.
        
        Returns:
            Dict[str, Dict]: Mapping of instrument symbol to configuration
        """
        # Get enabled instruments from expert settings
        enabled_instruments_setting = self.settings.get('enabled_instruments')

        if enabled_instruments_setting:
            # If it's already a dict, return it directly
            if isinstance(enabled_instruments_setting, dict):
                return enabled_instruments_setting
            # If it's a string, try to parse it as JSON
            elif isinstance(enabled_instruments_setting, str):
                try:
                    import json
                    return json.loads(enabled_instruments_setting)
                except (json.JSONDecodeError, ValueError):
                    logger.warning(f"Failed to parse enabled_instruments setting as JSON: {enabled_instruments_setting}")
                    return {}

        # Return empty dict if no enabled instruments configured
        return {}

    def get_virtual_balance(self) -> Optional[float]:
        """
        Get the virtual balance for this expert based on account balance and virtual_equity_pct.
        
        For example, if account balance is $10,000 and virtual_equity_pct is 10,
        the virtual balance would be $1,000 (10% of account balance).
        
        Returns:
            Optional[float]: The virtual balance amount, None if error occurred
        """
        try:
            # Lazy import to avoid circular dependency
            from ..utils import get_account_instance_from_id
            
            # Get the expert instance to access virtual_equity_pct
            expert_instance = get_instance(ExpertInstance, self.id)
            if not expert_instance:
                logger.error(f"Expert instance {self.id} not found")
                return None
            
            # Get the account instance for this expert
            account = get_account_instance_from_id(expert_instance.account_id)
            if not account:
                logger.error(f"Account {expert_instance.account_id} not found for expert {self.id}")
                return None
            
            # Get account balance
            account_balance = account.get_balance()
            if account_balance is None:
                logger.error(f"Could not get balance for account {expert_instance.account_id}")
                return None
            
            # Calculate virtual balance based on virtual_equity_pct
            virtual_equity_pct = expert_instance.virtual_equity_pct or 100.0
            virtual_balance = account_balance * (virtual_equity_pct / 100.0)
            
            logger.debug(f"Expert {self.id}: Account balance=${account_balance}, "
                        f"Virtual equity %={virtual_equity_pct}, Virtual balance=${virtual_balance}")
            
            return virtual_balance
            
        except Exception as e:
            logger.error(f"Error calculating virtual balance for expert {self.id}: {e}", exc_info=True)
            return None
    
    def get_available_balance(self) -> Optional[float]:
        """
        Get the available balance for this expert by calculating virtual balance minus used balance.
        
        The used balance calculation logic:
        - For profitable transactions: use open_price * quantity
        - For losing transactions: use (open_price + loss_amount) * quantity
        - This ensures we account for the maximum potential loss
        
        Returns:
            Optional[float]: The available balance amount, None if error occurred
        """
        try:
            # Get virtual balance first
            virtual_balance = self.get_virtual_balance()
            if virtual_balance is None:
                return None
            
            # Get expert instance and account instance to fetch current prices
            # Lazy import to avoid circular dependency
            from ..utils import get_account_instance_from_id
            
            expert_instance = get_instance(ExpertInstance, self.id)
            if not expert_instance:
                logger.error(f"Expert instance {self.id} not found")
                return None
                
            account = get_account_instance_from_id(expert_instance.account_id)
            if not account:
                logger.error(f"Account {expert_instance.account_id} not found for expert {self.id}")
                return None
            
            # Calculate used balance from open transactions
            used_balance = self._calculate_used_balance(account)
            if used_balance is None:
                return None
            
            available_balance = virtual_balance - used_balance
            
            logger.debug(f"Expert {self.id}: Virtual balance=${virtual_balance}, "
                        f"Used balance=${used_balance}, Available balance=${available_balance}")
            
            return available_balance
            
        except Exception as e:
            logger.error(f"Error calculating available balance for expert {self.id}: {e}", exc_info=True)
            return None
    
    def _calculate_used_balance(self, account: AccountInterface) -> Optional[float]:
        """
        Calculate the used balance from all open transactions for this expert. Uses bulk price fetching.
        
        Args:
            account: The account instance to get current prices
            
        Returns:
            Optional[float]: The total used balance, None if error occurred
        """
        try:
            used_balance = 0.0
            
            # Get all open transactions for this expert
            with Session(get_db().bind) as session:
                statement = select(Transaction).where(
                    Transaction.expert_id == self.id,
                    Transaction.status.in_([TransactionStatus.WAITING, TransactionStatus.OPENED])
                )
                transactions = session.exec(statement).all()
            
            if not transactions:
                return used_balance
            
            # Fetch all prices at once (bulk fetching)
            all_symbols = list(set(t.symbol for t in transactions))
            logger.debug(f"Fetching prices for {len(all_symbols)} symbols in bulk for used balance calculation")
            symbol_prices = account.get_instrument_current_price(all_symbols)
            
            for transaction in transactions:
                if transaction.open_price is None or transaction.quantity is None:
                    logger.warning(f"Transaction {transaction.id} missing open_price or quantity, skipping")
                    continue
                
                # Get current price from bulk-fetched prices
                current_price = symbol_prices.get(transaction.symbol) if symbol_prices else None
                if current_price is None:
                    logger.warning(f"Could not get current price for {transaction.symbol}, using open_price")
                    current_price = transaction.open_price
                
                # Calculate profit/loss
                if transaction.quantity > 0:  # Long position
                    profit_loss = (current_price - transaction.open_price) * transaction.quantity
                else:  # Short position  
                    profit_loss = (transaction.open_price - current_price) * abs(transaction.quantity)
                
                # Calculate used balance for this transaction
                if profit_loss >= 0:
                    # Transaction is profitable, use open_price
                    transaction_used = transaction.open_price * abs(transaction.quantity)
                else:
                    # Transaction is losing money, use open_price + loss
                    loss_amount = abs(profit_loss)
                    transaction_used = (transaction.open_price * abs(transaction.quantity)) + loss_amount
                
                used_balance += transaction_used
                
                logger.debug(f"Transaction {transaction.id} ({transaction.symbol}): "
                           f"Open=${transaction.open_price}, Current=${current_price}, "
                           f"P/L=${profit_loss:.2f}, Used=${transaction_used:.2f}")
            
            return used_balance
            
        except Exception as e:
            logger.error(f"Error calculating used balance for expert {self.id}: {e}", exc_info=True)
            return None

    def has_sufficient_balance_for_entry(self) -> bool:
        """
        Check if the expert has sufficient available balance to enter new market positions.
        
        Uses the min_available_balance_pct setting to determine if available balance
        meets the minimum threshold for new position entry.
        
        Returns:
            bool: True if sufficient balance available, False otherwise
        """
        try:
            # Get the minimum balance threshold from settings
            min_balance_pct = self.settings.get('min_available_balance_pct', 10.0)
            if min_balance_pct is None:
                min_balance_pct = 10.0
            
            # Get virtual and available balances using direct methods
            virtual_balance = self.get_virtual_balance()
            available_balance = self.get_available_balance()
            
            if virtual_balance is None or available_balance is None:
                logger.warning(f"Could not get balance information for expert {self.id}")
                return False
            
            # Calculate available balance percentage
            if virtual_balance <= 0:
                logger.warning(f"Expert {self.id} has zero or negative virtual balance")
                return False
            
            available_balance_pct = (available_balance / virtual_balance) * 100.0
            
            # Check if available balance meets minimum threshold (with None check)
            if min_balance_pct is None:
                logger.warning(f"Expert {self.id} has no minimum balance threshold set, defaulting to 20%")
                min_balance_pct = 20.0
            
            has_sufficient = available_balance_pct >= min_balance_pct
            
            logger.debug(f"Expert {self.id} balance check: Available={available_balance:.2f} "
                        f"({available_balance_pct:.1f}%), Virtual={virtual_balance:.2f}, "
                        f"Threshold={min_balance_pct:.1f}%, Sufficient={has_sufficient}")
            
            return has_sufficient
            
        except Exception as e:
            logger.error(f"Error checking balance for expert {self.id}: {e}", exc_info=True)
            return False

    def should_skip_analysis_for_symbol(self, symbol: str) -> tuple[bool, str]:
        """
        Check if analysis should be skipped for a symbol based on price and balance constraints.
        
        This function performs two checks:
        1. If symbol price is higher than available balance, skip analysis
        2. If available balance is lower than 5% of account balance, skip analysis
        
        Args:
            symbol (str): The instrument symbol to check
            
        Returns:
            tuple[bool, str]: (should_skip, reason)
                - should_skip: True if analysis should be skipped, False if it should proceed
                - reason: String explaining why analysis should be skipped (empty if should not skip)
        """
        try:
            # Get account instance
            from ..utils import get_account_instance_from_id
            from ..db import get_instance
            from ..models import ExpertInstance
            
            expert_instance = get_instance(ExpertInstance, self.id)
            if not expert_instance:
                logger.error(f"Expert instance {self.id} not found")
                return True, "Expert instance not found"
            
            account = get_account_instance_from_id(expert_instance.account_id)
            if not account:
                logger.error(f"Account {expert_instance.account_id} not found for expert {self.id}")
                return True, "Account not found"
            
            # Get current symbol price
            current_price = account.get_instrument_current_price(symbol)
            if current_price is None:
                logger.warning(f"Could not get current price for {symbol}, skipping analysis")
                return True, f"Could not get current price for {symbol}"
            
            # Get available balance for this expert
            available_balance = self.get_available_balance()
            if available_balance is None:
                logger.warning(f"Could not get available balance for expert {self.id}")
                return True, "Could not get available balance"
            
            # Check 1: If symbol price is higher than available balance, skip
            if current_price > available_balance:
                logger.info(f"Skipping analysis for {symbol}: price ${current_price:.2f} > available balance ${available_balance:.2f}")
                return True, f"Symbol price ${current_price:.2f} exceeds available balance ${available_balance:.2f}"
            
            # Check 2: If available balance is lower than 5% of account balance, skip
            account_balance = account.get_balance()
            if account_balance is None:
                logger.warning(f"Could not get account balance, skipping 5% check")
                return True, "Could not get account balance for 5% check"
            
            min_balance_threshold = account_balance * 0.05  # 5% of account balance
            if available_balance < min_balance_threshold:
                available_pct = (available_balance / account_balance) * 100.0 if account_balance > 0 else 0.0
                logger.info(f"Skipping analysis for {symbol}: available balance ${available_balance:.2f} "
                           f"({available_pct:.1f}%) < 5% threshold ${min_balance_threshold:.2f}")
                return True, f"Available balance ${available_balance:.2f} ({available_pct:.1f}%) below 5% threshold"
            
            # All checks passed - analysis should proceed
            logger.debug(f"Analysis checks passed for {symbol}: price=${current_price:.2f}, "
                        f"available=${available_balance:.2f}, account_balance=${account_balance:.2f}")
            return False, ""
            
        except Exception as e:
            logger.error(f"Error checking if analysis should be skipped for {symbol}: {e}", exc_info=True)
            return True, f"Error during analysis check: {str(e)}"

    def get_recommended_instruments(self) -> Optional[List[str]]:
        """
        Get expert-recommended instruments for analysis.
        This method is only called if the expert's can_recommend_instruments property is True
        and instrument_selection_method is set to "expert".
        
        Returns:
            Optional[List[str]]: List of recommended instrument symbols, or None if not supported
        """
        return None

    @abstractmethod
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run analysis for a specific symbol and market analysis instance.
        This method should update the market_analysis object with results.
        
        Args:
            symbol (str): The instrument symbol to analyze.
            market_analysis (MarketAnalysis): The market analysis instance to update with results.
        """
        pass

    @classmethod
    def get_expert_actions(cls) -> List[Dict[str, Any]]:
        """
        Get list of expert-specific actions that can be performed.
        
        Actions will be displayed in a separate tab in the expert settings UI.
        Each action should have:
        - name: Unique identifier for the action
        - label: Display name shown to user
        - description: What the action does
        - icon: Optional icon name (NiceGUI icon)
        - callback: Method name to call (must exist on expert instance)
        
        Returns:
            List[Dict[str, Any]]: List of action dictionaries. Empty list if no actions.
        
        Example:
            return [
                {
                    "name": "clear_memory",
                    "label": "Clear Memory",
                    "description": "Delete stored memory collections for this expert",
                    "icon": "delete",
                    "callback": "clear_memory_action"
                }
            ]
        """
        return []


   


   