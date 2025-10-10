"""
Alpha Vantage Common Utilities

Shared utilities for all Alpha Vantage data providers.
Provides API request handling, rate limit management, and data formatting.
"""

import os
import requests
import pandas as pd
import json
from datetime import datetime
from io import StringIO
from typing import Optional

from ba2_trade_platform.config import ALPHA_VANTAGE_API_KEY
from ba2_trade_platform.logger import logger

API_BASE_URL = "https://www.alphavantage.co/query"


class AlphaVantageRateLimitError(Exception):
    """Exception raised when Alpha Vantage API rate limit is exceeded."""
    pass


class AlphaVantageBaseProvider:
    """
    Base class for all Alpha Vantage data providers.
    
    This class provides common functionality for Alpha Vantage API calls,
    including source tracking for better usage analytics.
    
    Child classes should call super().__init__(source) in their constructors.
    """
    
    def __init__(self, source: str = "ba2_trade_platform"):
        """
        Initialize the Alpha Vantage base provider.
        
        Args:
            source: Source identifier for API tracking (e.g., 'ba2_trade_platform', 'trading_agents')
        """
        self.source = source
        logger.debug(f"AlphaVantageBaseProvider initialized with source: {source}")
    
    def make_api_request(self, function_name: str, params: dict) -> dict | str:
        """
        Make API request to Alpha Vantage with source tracking.
        
        This is an instance method that uses the provider's configured source.
        
        Args:
            function_name: Alpha Vantage function name (e.g., 'TIME_SERIES_DAILY_ADJUSTED')
            params: Additional parameters for the API request
            
        Returns:
            API response as string (CSV) or dict (JSON)
            
        Raises:
            AlphaVantageRateLimitError: When API rate limit is exceeded
            requests.HTTPError: When HTTP request fails
        """
        return make_api_request(function_name, params, source=self.source)


def get_api_key() -> str:
    """Retrieve the API key for Alpha Vantage from configuration."""
    if not ALPHA_VANTAGE_API_KEY:
        raise ValueError("ALPHA_VANTAGE_API_KEY is not set in BA2 Platform configuration.")
    return ALPHA_VANTAGE_API_KEY


def format_datetime_for_api(date_input) -> str:
    """
    Convert various date formats to YYYYMMDDTHHMM format required by Alpha Vantage API.
    
    Args:
        date_input: String or datetime object to convert
        
    Returns:
        Formatted date string (e.g., '20231225T0000')
        
    Raises:
        ValueError: If date format is not supported
    """
    if isinstance(date_input, str):
        # If already in correct format, return as-is
        if len(date_input) == 13 and 'T' in date_input:
            return date_input
        # Try to parse common date formats
        try:
            dt = datetime.strptime(date_input, "%Y-%m-%d")
            return dt.strftime("%Y%m%dT0000")
        except ValueError:
            try:
                dt = datetime.strptime(date_input, "%Y-%m-%d %H:%M")
                return dt.strftime("%Y%m%dT%H%M")
            except ValueError:
                raise ValueError(f"Unsupported date format: {date_input}")
    elif isinstance(date_input, datetime):
        return date_input.strftime("%Y%m%dT%H%M")
    else:
        raise ValueError(f"Date must be string or datetime object, got {type(date_input)}")


def make_api_request(function_name: str, params: dict, source: str = "ba2_trade_platform") -> dict | str:
    """
    Make API request to Alpha Vantage.
    
    Args:
        function_name: Alpha Vantage function name (e.g., 'TIME_SERIES_DAILY_ADJUSTED')
        params: Additional parameters for the API request
        source: Source identifier for API tracking (default: 'ba2_trade_platform')
        
    Returns:
        API response as string (CSV) or dict (JSON)
        
    Raises:
        AlphaVantageRateLimitError: When API rate limit is exceeded
        requests.HTTPError: When HTTP request fails
    """
    # Create a copy of params to avoid modifying the original
    api_params = params.copy()
    api_params.update({
        "function": function_name,
        "apikey": get_api_key(),
        "source": source,
    })
    
    # Log the request
    logger.debug(f"Alpha Vantage API request: function={function_name}, params={params}")
    
    response = requests.get(API_BASE_URL, params=api_params)
    response.raise_for_status()

    response_text = response.text
    
    # Check if response is JSON (error responses are typically JSON)
    try:
        response_json = json.loads(response_text)
        # Check for rate limit error
        if "Information" in response_json:
            info_message = response_json["Information"]
            if "rate limit" in info_message.lower() or "api key" in info_message.lower():
                logger.error(f"Alpha Vantage rate limit exceeded: {info_message}")
                raise AlphaVantageRateLimitError(f"Alpha Vantage rate limit exceeded: {info_message}")
        
        logger.debug(f"Alpha Vantage API response: JSON with {len(response_json)} keys")
        return response_json
    except json.JSONDecodeError:
        # Response is not JSON (likely CSV data), which is normal
        logger.debug(f"Alpha Vantage API response: CSV data ({len(response_text)} chars)")
        pass

    return response_text


def filter_csv_by_date_range(csv_data: str, start_date: str, end_date: str) -> str:
    """
    Filter CSV data to include only rows within the specified date range.

    Args:
        csv_data: CSV string from Alpha Vantage API
        start_date: Start date in yyyy-mm-dd format
        end_date: End date in yyyy-mm-dd format

    Returns:
        Filtered CSV string
        
    Example:
        >>> csv_data = "timestamp,open,high,low,close\\n2024-01-01,100,110,90,105\\n"
        >>> filtered = filter_csv_by_date_range(csv_data, "2024-01-01", "2024-01-31")
    """
    if not csv_data or csv_data.strip() == "":
        logger.debug("Empty CSV data, returning as-is")
        return csv_data

    try:
        # Parse CSV data
        df = pd.read_csv(StringIO(csv_data))

        # Assume the first column is the date column (timestamp)
        date_col = df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col])

        # Filter by date range
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)

        filtered_df = df[(df[date_col] >= start_dt) & (df[date_col] <= end_dt)]

        logger.debug(
            f"Filtered CSV from {len(df)} to {len(filtered_df)} rows "
            f"(date range: {start_date} to {end_date})"
        )

        # Convert back to CSV string
        return filtered_df.to_csv(index=False)

    except Exception as e:
        # If filtering fails, return original data with a warning
        logger.warning(f"Failed to filter CSV data by date range: {e}")
        return csv_data
