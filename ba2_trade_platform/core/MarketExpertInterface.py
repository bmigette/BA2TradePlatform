from abc import abstractmethod
from typing import Any, Dict, Optional
from unittest import result
from ..logger import logger
from ..core.models import ExpertSetting
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

    @abstractmethod
    def get_prediction_for_instrument(self, instrument: str) -> Any:
        """
        Get predictions for a single instrument (symbol).
        Args:
            instrument (str): The instrument symbol or identifier.
        Returns:
            Any: The prediction result for the instrument.
        """
        pass

    @abstractmethod
    def get_predictions_for_all_enabled_instruments(self) -> Dict[str, Any]:
        """
        Get predictions for all enabled instruments.
        Returns:
            Dict[str, Any]: Mapping of instrument symbol to prediction result.
        """
        pass

    @abstractmethod
    def get_supported_instruments(self) -> list:
        """
        Get a list of all supported instruments for this expert.
        Returns:
            list: List of supported instrument symbols/identifiers.
        """
        pass

    @abstractmethod
    def get_enabled_instruments(self) -> list:
        """
        Get a list of all enabled instruments for this expert instance.
        Returns:
            list: List of enabled instrument symbols/identifiers.
        """
        pass



   