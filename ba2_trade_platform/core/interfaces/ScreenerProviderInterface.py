"""
Base interface for stock screener providers in the BA2 Trade Platform.

Screener providers allow filtering and discovering stocks based on criteria
such as price range, volume, market cap, sector, and exchange.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List


class ScreenerProviderInterface(ABC):
    """
    Base interface for stock screener providers.

    Implementations should query external APIs to screen stocks matching
    the given filter criteria and return normalised result dicts.
    """

    @abstractmethod
    def get_provider_name(self) -> str:
        """
        Return the provider name.

        Returns:
            str: Provider name (e.g., 'fmp')
        """
        pass

    @abstractmethod
    def screen_stocks(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Screen stocks matching filters.

        Filters:
            price_min (float): Minimum stock price
            price_max (float): Maximum stock price
            volume_min (int): Minimum average volume
            market_cap_min (int): Minimum market capitalisation
            market_cap_max (int): Maximum market capitalisation
            float_max (int): Maximum share float
            exchanges (List[str]): List of exchanges to include
            sector_exclude (List[str]): List of sectors to exclude
            limit (int): Maximum number of results

        Returns:
            List of dicts with keys: symbol, company_name, price, volume,
            market_cap, sector, exchange (at minimum).
        """
        pass

    @abstractmethod
    def validate_config(self) -> bool:
        """
        Validate provider configuration (API keys, credentials, etc.).

        Returns:
            bool: True if configuration is valid and provider is ready to use,
                 False otherwise
        """
        pass
