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

    @classmethod
    @abstractmethod
    def description(cls) -> str:
        """
        Abstract property for a human-readable description of the expert.
        Returns:
            str: Description of the expert instance.
        """
        pass


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



   