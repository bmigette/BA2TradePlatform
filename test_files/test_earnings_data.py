"""
Test Script for Earnings Data Providers

Tests the get_past_earnings() and get_earnings_estimates() methods for:
- YFinanceCompanyDetailsProvider
- FMPCompanyDetailsProvider
- AlphaVantageCompanyDetailsProvider

Usage:
    .venv\Scripts\python.exe test_files\test_earnings_data.py
"""

from datetime import datetime, timedelta
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.modules.dataproviders.fundamentals.details import (
    YFinanceCompanyDetailsProvider,
    FMPCompanyDetailsProvider,
    AlphaVantageCompanyDetailsProvider
)
from ba2_trade_platform.logger import logger


def print_separator(title: str):
    """Print a formatted separator."""
    print("\n" + "="*80)
    print(f" {title}")
    print("="*80 + "\n")


def test_provider_earnings(provider_name: str, provider_class, test_symbol: str = "AAPL"):
    """
    Test earnings methods for a given provider.
    
    Args:
        provider_name: Name of the provider for display
        provider_class: Provider class to instantiate
        test_symbol: Stock symbol to test with
    """
    print_separator(f"Testing {provider_name}")
    
    try:
        # Initialize provider
        print(f"Initializing {provider_name}...")
        provider = provider_class()
        print(f"✓ Provider initialized successfully")
        print(f"  Supported features: {provider.get_supported_features()}")
        
        # Test get_past_earnings (quarterly, last 8 quarters = 2 years)
        print(f"\n--- Testing get_past_earnings() ---")
        print(f"Symbol: {test_symbol}")
        print(f"Frequency: quarterly")
        print(f"Lookback periods: 8 (2 years)")
        
        end_date = datetime.now()
        past_earnings_dict = provider.get_past_earnings(
            symbol=test_symbol,
            frequency="quarterly",
            end_date=end_date,
            lookback_periods=8,
            format_type="dict"
        )
        
        if "error" in past_earnings_dict:
            print(f"✗ Error: {past_earnings_dict['error']}")
        else:
            earnings_count = len(past_earnings_dict.get("earnings", []))
            print(f"✓ Retrieved {earnings_count} past earnings periods")
            
            if earnings_count > 0:
                print(f"\nMost recent earnings:")
                recent = past_earnings_dict["earnings"][0]
                print(f"  Date: {recent['fiscal_date_ending']}")
                print(f"  Reported EPS: ${recent['reported_eps']:.2f}")
                print(f"  Estimated EPS: ${recent['estimated_eps']:.2f}")
                if recent.get('surprise') is not None:
                    print(f"  Surprise: ${recent['surprise']:.2f} ({recent['surprise_percent']:.1f}%)")
                
                # Show markdown format sample
                print(f"\nMarkdown format (first 500 chars):")
                past_earnings_md = provider.get_past_earnings(
                    symbol=test_symbol,
                    frequency="quarterly",
                    end_date=end_date,
                    lookback_periods=8,
                    format_type="markdown"
                )
                print(past_earnings_md[:500] + "...")
        
        # Test get_earnings_estimates (quarterly, next 4 quarters)
        print(f"\n--- Testing get_earnings_estimates() ---")
        print(f"Symbol: {test_symbol}")
        print(f"Frequency: quarterly")
        print(f"Forward periods: 4")
        
        as_of_date = datetime.now()
        estimates_dict = provider.get_earnings_estimates(
            symbol=test_symbol,
            frequency="quarterly",
            as_of_date=as_of_date,
            lookback_periods=4,
            format_type="dict"
        )
        
        if "error" in estimates_dict:
            print(f"✗ Error: {estimates_dict['error']}")
        else:
            estimates_count = len(estimates_dict.get("estimates", []))
            print(f"✓ Retrieved {estimates_count} earnings estimates")
            
            if estimates_count > 0:
                print(f"\nNext earnings estimate:")
                next_est = estimates_dict["estimates"][0]
                print(f"  Date: {next_est['fiscal_date_ending']}")
                print(f"  Avg Estimate: ${next_est['estimated_eps_avg']:.2f}")
                print(f"  High Estimate: ${next_est['estimated_eps_high']:.2f}")
                print(f"  Low Estimate: ${next_est['estimated_eps_low']:.2f}")
                print(f"  # Analysts: {next_est['number_of_analysts']}")
                
                # Show markdown format sample
                print(f"\nMarkdown format (first 500 chars):")
                estimates_md = provider.get_earnings_estimates(
                    symbol=test_symbol,
                    frequency="quarterly",
                    as_of_date=as_of_date,
                    lookback_periods=4,
                    format_type="markdown"
                )
                print(estimates_md[:500] + "...")
        
        print(f"\n✓ {provider_name} tests completed successfully")
        return True
        
    except Exception as e:
        print(f"\n✗ {provider_name} tests failed: {e}")
        logger.error(f"Error testing {provider_name}", exc_info=True)
        return False


def main():
    """Main test function."""
    print_separator("Earnings Data Providers Test Suite")
    print("This script tests the new earnings data methods across all providers.")
    print("Test symbol: AAPL (Apple Inc.)")
    
    # Test configuration
    test_symbol = "AAPL"
    results = {}
    
    # Test each provider
    providers = [
        ("YFinance", YFinanceCompanyDetailsProvider),
        ("FMP (Financial Modeling Prep)", FMPCompanyDetailsProvider),
        ("AlphaVantage", AlphaVantageCompanyDetailsProvider),
    ]
    
    for provider_name, provider_class in providers:
        try:
            success = test_provider_earnings(provider_name, provider_class, test_symbol)
            results[provider_name] = "PASS" if success else "FAIL"
        except Exception as e:
            print(f"\n✗ Unexpected error testing {provider_name}: {e}")
            logger.error(f"Unexpected error in {provider_name}", exc_info=True)
            results[provider_name] = "ERROR"
    
    # Print summary
    print_separator("Test Results Summary")
    for provider_name, result in results.items():
        status_symbol = "✓" if result == "PASS" else "✗"
        print(f"{status_symbol} {provider_name}: {result}")
    
    # Overall result
    all_passed = all(r == "PASS" for r in results.values())
    print("\n" + "="*80)
    if all_passed:
        print("✓ ALL TESTS PASSED")
    else:
        print("✗ SOME TESTS FAILED")
    print("="*80 + "\n")
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
