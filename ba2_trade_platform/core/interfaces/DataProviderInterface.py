"""
Base interface for all data providers in the BA2 Trade Platform.

This interface defines the common contract that all data providers must implement,
including provider identification, feature listing, configuration validation,
and response formatting (both structured dict and LLM-friendly markdown).
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Literal


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
