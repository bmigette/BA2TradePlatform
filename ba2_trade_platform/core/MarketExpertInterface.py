from abc import abstractmethod
from typing import Any, Dict, List, Optional
from unittest import result
from ..logger import logger
from ..core.models import ExpertSetting, MarketAnalysis
from ..core.ExtendableSettingsInterface import ExtendableSettingsInterface


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
        
        # Ensure builtin settings are initialized
        self._ensure_builtin_settings()
    
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
                "automatic_trading": {
                    "type": "bool", "required": False, "default": True,
                    "description": "Enable automatic trade execution"
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
        
        Returns:
            List[str]: List of enabled instrument symbols/identifiers.
        """
        #logger.debug('Getting enabled instruments from settings')
        
        try:
            # Get enabled instruments from expert settings
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
            logger.error(f'Error getting enabled instruments: {e}')
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



   