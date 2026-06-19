#!/usr/bin/env python3
"""
Test script to compare fundamentals data from different providers.

This script tests what data each provider can actually return and compares
the field names and structure.
"""

import os
import sys
from datetime import datetime, timedelta
from pprint import pprint

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

SYMBOL = "AAPL"
END_DATE = datetime.now()
START_DATE = END_DATE - timedelta(days=365)
LOOKBACK_PERIODS = 4


def test_yfinance():
    """Test YFinance provider capabilities."""
    print("\n" + "="*60)
    print("YFINANCE PROVIDER")
    print("="*60)

    try:
        from ba2_providers.fundamentals.details import YFinanceCompanyDetailsProvider
        provider = YFinanceCompanyDetailsProvider()

        # Test balance sheet
        print("\n--- Balance Sheet ---")
        try:
            result = provider.get_balance_sheet(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "periods" in result:
                print(f"Periods: {len(result['periods'])}")
                if result['periods']:
                    print(f"Sample fields: {list(result['periods'][0].get('items', {}).keys())[:10]}")
            else:
                print(f"Result: {result}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test income statement
        print("\n--- Income Statement ---")
        try:
            result = provider.get_income_statement(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "periods" in result:
                print(f"Periods: {len(result['periods'])}")
                if result['periods']:
                    print(f"Sample fields: {list(result['periods'][0].get('items', {}).keys())[:10]}")
            else:
                print(f"Result: {result}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test cash flow statement
        print("\n--- Cash Flow Statement ---")
        try:
            result = provider.get_cashflow_statement(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "periods" in result:
                print(f"Periods: {len(result['periods'])}")
                if result['periods']:
                    print(f"Sample fields: {list(result['periods'][0].get('items', {}).keys())[:10]}")
            else:
                print(f"Result: {result}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test past earnings
        print("\n--- Past Earnings ---")
        try:
            result = provider.get_past_earnings(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "earnings" in result:
                print(f"Earnings: {len(result['earnings'])}")
                if result['earnings']:
                    print(f"Sample: {result['earnings'][0]}")
            else:
                print(f"Result: {result}")
        except Exception as e:
            print(f"ERROR: {e}")

    except Exception as e:
        print(f"Failed to initialize YFinance provider: {e}")


def test_fmp():
    """Test FMP provider capabilities."""
    print("\n" + "="*60)
    print("FMP PROVIDER")
    print("="*60)

    if not os.getenv("FMP_API_KEY"):
        print("FMP_API_KEY not set, skipping FMP tests")
        return

    try:
        from ba2_providers.fundamentals.details import FMPCompanyDetailsProvider
        provider = FMPCompanyDetailsProvider()

        # Test balance sheet
        print("\n--- Balance Sheet ---")
        try:
            result = provider.get_balance_sheet(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "statements" in result:
                print(f"Statements: {len(result['statements'])}")
                if result['statements']:
                    print(f"Sample fields: {list(result['statements'][0].keys())[:10]}")
            else:
                print(f"Result type: {type(result)}")
                if isinstance(result, str):
                    print(f"Result (first 200 chars): {result[:200]}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test income statement
        print("\n--- Income Statement ---")
        try:
            result = provider.get_income_statement(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "statements" in result:
                print(f"Statements: {len(result['statements'])}")
                if result['statements']:
                    print(f"Sample fields: {list(result['statements'][0].keys())[:10]}")
            else:
                print(f"Result type: {type(result)}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test cash flow statement
        print("\n--- Cash Flow Statement ---")
        try:
            result = provider.get_cashflow_statement(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "statements" in result:
                print(f"Statements: {len(result['statements'])}")
                if result['statements']:
                    print(f"Sample fields: {list(result['statements'][0].keys())[:10]}")
            else:
                print(f"Result type: {type(result)}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test past earnings
        print("\n--- Past Earnings ---")
        try:
            result = provider.get_past_earnings(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "earnings" in result:
                print(f"Earnings: {len(result['earnings'])}")
                if result['earnings']:
                    print(f"Sample: {result['earnings'][0]}")
            else:
                print(f"Result: {result}")
        except Exception as e:
            print(f"ERROR: {e}")

    except Exception as e:
        print(f"Failed to initialize FMP provider: {e}")


def test_alphavantage():
    """Test AlphaVantage provider capabilities."""
    print("\n" + "="*60)
    print("ALPHAVANTAGE PROVIDER")
    print("="*60)

    if not os.getenv("ALPHA_VANTAGE_API_KEY"):
        print("ALPHA_VANTAGE_API_KEY not set, skipping AlphaVantage tests")
        return

    try:
        from ba2_providers.fundamentals.details import AlphaVantageCompanyDetailsProvider
        provider = AlphaVantageCompanyDetailsProvider()

        # Test balance sheet
        print("\n--- Balance Sheet ---")
        try:
            result = provider.get_balance_sheet(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "statements" in result:
                print(f"Statements: {len(result['statements'])}")
                if result['statements']:
                    print(f"Sample fields: {list(result['statements'][0].keys())[:10]}")
            else:
                print(f"Result type: {type(result)}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test income statement
        print("\n--- Income Statement ---")
        try:
            result = provider.get_income_statement(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "statements" in result:
                print(f"Statements: {len(result['statements'])}")
                if result['statements']:
                    print(f"Sample fields: {list(result['statements'][0].keys())[:10]}")
            else:
                print(f"Result type: {type(result)}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test cash flow statement
        print("\n--- Cash Flow Statement ---")
        try:
            result = provider.get_cashflow_statement(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "statements" in result:
                print(f"Statements: {len(result['statements'])}")
                if result['statements']:
                    print(f"Sample fields: {list(result['statements'][0].keys())[:10]}")
            else:
                print(f"Result type: {type(result)}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test past earnings
        print("\n--- Past Earnings ---")
        try:
            result = provider.get_past_earnings(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS,
                format_type="dict"
            )
            if isinstance(result, dict) and "earnings" in result:
                print(f"Earnings: {len(result['earnings'])}")
                if result['earnings']:
                    print(f"Sample: {result['earnings'][0]}")
            else:
                print(f"Result: {result}")
        except Exception as e:
            print(f"ERROR: {e}")

    except Exception as e:
        print(f"Failed to initialize AlphaVantage provider: {e}")


def print_env_status():
    """Print environment variable status."""
    print("\n" + "="*60)
    print("ENVIRONMENT STATUS")
    print("="*60)
    print(f"FMP_API_KEY: {'SET' if os.getenv('FMP_API_KEY') else 'NOT SET'}")
    print(f"ALPHA_VANTAGE_API_KEY: {'SET' if os.getenv('ALPHA_VANTAGE_API_KEY') else 'NOT SET'}")


def test_fundamentals_service():
    """Test the unified FundamentalsService with provider fallback."""
    print("\n" + "="*60)
    print("FUNDAMENTALS SERVICE (UNIFIED)")
    print("="*60)

    try:
        from ba2_providers.fundamentals.service import FundamentalsService

        # Test with priority order: yfinance first, then fmp
        service = FundamentalsService(providers=['yfinance', 'fmp', 'alphavantage'])

        # Test balance sheet
        print("\n--- Balance Sheet (with fallback) ---")
        try:
            result = service.get_balance_sheet(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS
            )
            print(f"Provider used: {result.provider}")
            print(f"Periods: {result.period_count}")
            if result.periods:
                print(f"Normalized fields: {list(result.periods[0].keys())[:8]}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test income statement
        print("\n--- Income Statement (with fallback) ---")
        try:
            result = service.get_income_statement(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS
            )
            print(f"Provider used: {result.provider}")
            print(f"Periods: {result.period_count}")
            if result.periods:
                print(f"Normalized fields: {list(result.periods[0].keys())[:8]}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test cash flow
        print("\n--- Cash Flow (with fallback) ---")
        try:
            result = service.get_cash_flow(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS
            )
            print(f"Provider used: {result.provider}")
            print(f"Periods: {result.period_count}")
            if result.periods:
                print(f"Normalized fields: {list(result.periods[0].keys())[:8]}")
        except Exception as e:
            print(f"ERROR: {e}")

        # Test earnings
        print("\n--- Earnings (with fallback) ---")
        try:
            result = service.get_earnings(
                symbol=SYMBOL,
                frequency="quarterly",
                end_date=END_DATE,
                lookback_periods=LOOKBACK_PERIODS
            )
            print(f"Provider used: {result.provider}")
            print(f"Periods: {result.period_count}")
            if result.periods:
                print(f"Sample: {result.periods[0]}")
        except Exception as e:
            print(f"ERROR: {e}")

    except Exception as e:
        print(f"Failed to test FundamentalsService: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print("Testing Fundamentals Providers")
    print(f"Symbol: {SYMBOL}")
    print(f"Date Range: {START_DATE.date()} to {END_DATE.date()}")
    print(f"Lookback Periods: {LOOKBACK_PERIODS}")

    print_env_status()

    test_yfinance()
    test_fmp()
    test_alphavantage()
    test_fundamentals_service()

    print("\n" + "="*60)
    print("DONE")
    print("="*60)
