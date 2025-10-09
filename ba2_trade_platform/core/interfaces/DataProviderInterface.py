"""
Base interface for all data providers in the BA2 Trade Platform.

This interface defines the common contract that all data providers must implement,
including provider identification, feature listing, configuration validation,
and response formatting (both structured dict and LLM-friendly markdown).
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Literal
from datetime import datetime


class DataProviderInterface(ABC):
    """
    Base interface for all data providers.
    
    All data provider interfaces (market indicators, fundamentals, news, etc.)
    should extend this base interface to ensure consistent behavior across providers.
    """
    
    @abstractmethod
    def get_provider_name(self) -> str:
        """
        Return the provider name.
        
        Returns:
            str: Provider name (e.g., 'alpaca', 'yfinance', 'alphavantage')
        """
        pass
    
    @abstractmethod
    def get_supported_features(self) -> list[str]:
        """
        Return list of supported features/methods by this provider.
        
        Returns:
            list[str]: List of feature names this provider supports
                      (e.g., ['company_news', 'global_news'] for a news provider)
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
    
    def format_response(
        self, 
        data: Any, 
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Format response in the requested format.
        
        This method provides a consistent interface for formatting data in either
        structured dictionary format or markdown format for LLM consumption.
        
        Args:
            data: Raw data to format (provider-specific format)
            format_type: Either "dict" for structured data or "markdown" for LLM consumption
            
        Returns:
            Dict[str, Any] | str: Formatted data in the requested format
        """
        if format_type == "dict":
            return self._format_as_dict(data)
        else:
            return self._format_as_markdown(data)
    
    @abstractmethod
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """
        Format data as a structured dictionary.
        
        Args:
            data: Raw data from the provider API
            
        Returns:
            Dict[str, Any]: Structured dictionary with standardized keys
        """
        pass
    
    @abstractmethod
    def _format_as_markdown(self, data: Any) -> str:
        """
        Format data as markdown for LLM consumption.
        
        Args:
            data: Raw data from the provider API
            
        Returns:
            str: Markdown-formatted string suitable for LLM processing
        """
        pass
    
    @staticmethod
    def format_datetime_for_dict(dt: datetime) -> str:
        """
        Format datetime as ISO string for dict output.
        
        Args:
            dt: Datetime object
            
        Returns:
            str: ISO format datetime string (e.g., "2024-01-15T14:30:00")
        """
        if dt is None:
            return None
        if isinstance(dt, str):
            return dt
        return dt.isoformat()
    
    @staticmethod
    def format_datetime_for_markdown(dt: datetime, interval: str = "1d") -> str:
        """
        Format datetime for markdown display.
        For daily (1d) and higher intervals, show date only.
        For intraday intervals, show date and time.
        
        Args:
            dt: Datetime object
            interval: Data interval (e.g., "1m", "1h", "1d", "1wk")
            
        Returns:
            str: Formatted datetime string
        """
        if dt is None:
            return ""
        
        # Convert string to datetime if needed
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
            except:
                return dt
        
        # Check if interval is daily or higher (1d, 1wk, 1mo)
        if interval and any(interval.endswith(suffix) for suffix in ['d', 'wk', 'mo']):
            # Date only for daily and higher
            return dt.strftime("%Y-%m-%d")
        else:
            # Date and time for intraday
            return dt.strftime("%Y-%m-%d %H:%M:%S")
