"""
Macroeconomic Data Providers

Providers for macroeconomic indicators, yield curves, and Fed calendar data

Available Providers:
    - FRED: Federal Reserve Economic Data (FRED) API
"""

from .FREDMacroProvider import FREDMacroProvider

__all__ = [
    "FREDMacroProvider",
]
