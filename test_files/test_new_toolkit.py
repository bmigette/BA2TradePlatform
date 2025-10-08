"""
Comprehensive test suite for the new TradingAgents toolkit with BA2 provider integration.

This test file validates:
1. Multi-provider aggregation for news, insider, macro, and fundamentals
2. Fallback logic for OHLCV and indicators
3. Error handling and provider attribution
4. Type annotations and parameter validation
5. End-to-end integration with real API calls

Requirements:
- Database must have valid API keys configured for providers
- Internet connection required for API calls
- Provider settings must be configured in expert instance
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Type
import traceback
import importlib.util

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.logger import logger

# Import the new toolkit directly by file path to avoid package import issues
toolkit_path = project_root / "ba2_trade_platform" / "thirdparties" / "TradingAgents" / "tradingagents" / "agents" / "utils" / "agent_utils_new.py"
spec = importlib.util.spec_from_file_location("agent_utils_new", toolkit_path)
agent_utils_new = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_utils_new)
Toolkit = agent_utils_new.Toolkit

# Try to import provider registries, but continue if they fail
try:
    from ba2_trade_platform.modules.dataproviders import (
        OHLCV_PROVIDERS,
        INDICATORS_PROVIDERS,
        FUNDAMENTALS_DETAILS_PROVIDERS,
        NEWS_PROVIDERS,
        MACRO_PROVIDERS,
        INSIDER_PROVIDERS,
        FREDMacroProvider,
        DataProviderInterface
    )
    PROVIDERS_AVAILABLE = True
except Exception as e:
    logger.warning(f"Could not import provider registries: {e}")
    logger.warning("Will create minimal provider map for testing")
    PROVIDERS_AVAILABLE = False
    OHLCV_PROVIDERS = {}
    INDICATORS_PROVIDERS = {}
    FUNDAMENTALS_DETAILS_PROVIDERS = {}
    NEWS_PROVIDERS = {}
    MACRO_PROVIDERS = {}
    INSIDER_PROVIDERS = {}
    FREDMacroProvider = None
    DataProviderInterface = None


class ToolkitTester:
    """Test harness for new toolkit with provider integration."""
    
    def __init__(self):
        """Initialize test harness."""
        self.test_symbol = "AAPL"
        self.test_end_date = datetime.now().strftime("%Y-%m-%d")
        self.test_start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        self.test_lookback_days = 7
        self.results = {
            "passed": 0,
            "failed": 0,
            "errors": []
        }
        
    def build_test_provider_map(self) -> Dict[str, List[Type]]:
        """Build a comprehensive provider map for testing."""
        if not PROVIDERS_AVAILABLE:
            logger.error("Providers not available - cannot build provider map")
            return {}
            
        provider_map = {}
        
        # OHLCV providers - try multiple for fallback testing
        ohlcv_providers = []
        for vendor_name in ["eodhd", "polygon", "fmp", "yfinance"]:
            if vendor_name in OHLCV_PROVIDERS:
                ohlcv_providers.append(OHLCV_PROVIDERS[vendor_name])
        if ohlcv_providers:
            provider_map["ohlcv"] = ohlcv_providers
            
        # Indicator providers
        indicator_providers = []
        for vendor_name in ["eodhd", "fmp"]:
            if vendor_name in INDICATORS_PROVIDERS:
                indicator_providers.append(INDICATORS_PROVIDERS[vendor_name])
        if indicator_providers:
            provider_map["indicators"] = indicator_providers
            
        # News providers - use all available for aggregation testing
        news_providers = []
        for vendor_name in ["eodhd", "polygon", "finnhub", "fmp"]:
            if vendor_name in NEWS_PROVIDERS:
                news_providers.append(NEWS_PROVIDERS[vendor_name])
        if news_providers:
            provider_map["news"] = news_providers
            
        # Fundamentals providers
        fundamentals_providers = []
        for vendor_name in ["eodhd", "fmp", "alphavantage"]:
            if vendor_name in FUNDAMENTALS_DETAILS_PROVIDERS:
                fundamentals_providers.append(FUNDAMENTALS_DETAILS_PROVIDERS[vendor_name])
        if fundamentals_providers:
            provider_map["fundamentals_details"] = fundamentals_providers
            
        # Insider providers
        insider_providers = []
        for vendor_name in ["eodhd", "fmp"]:
            if vendor_name in INSIDER_PROVIDERS:
                insider_providers.append(INSIDER_PROVIDERS[vendor_name])
        if insider_providers:
            provider_map["insider"] = insider_providers
            
        # Macro providers - default to FRED
        provider_map["macro"] = [FREDMacroProvider]
        
        logger.info(f"Built test provider map with {len(provider_map)} categories")
        for category, providers in provider_map.items():
            logger.info(f"  {category}: {[p.__name__ for p in providers]}")
            
        return provider_map
    
    def run_test(self, test_name: str, test_func):
        """Run a single test and track results."""
        logger.info(f"\n{'='*80}")
        logger.info(f"Running test: {test_name}")
        logger.info(f"{'='*80}")
        
        try:
            result = test_func()
            if result:
                logger.info(f"✅ PASSED: {test_name}")
                self.results["passed"] += 1
                return True
            else:
                logger.error(f"❌ FAILED: {test_name}")
                self.results["failed"] += 1
                self.results["errors"].append(f"{test_name}: Test returned False")
                return False
        except Exception as e:
            logger.error(f"❌ ERROR: {test_name}")
            logger.error(f"Exception: {str(e)}")
            logger.error(traceback.format_exc())
            self.results["failed"] += 1
            self.results["errors"].append(f"{test_name}: {str(e)}")
            return False
    
    def test_ohlcv_data_fallback(self, toolkit: Toolkit) -> bool:
        """Test OHLCV data retrieval with fallback logic."""
        logger.info(f"Testing OHLCV data for {self.test_symbol}")
        
        result = toolkit.get_ohlcv_data(
            symbol=self.test_symbol,
            start_date=self.test_start_date,
            end_date=self.test_end_date,
            interval="1d"
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for data presence (should have date and price columns)
        if "date" not in result.lower() and "close" not in result.lower():
            logger.error("Result doesn't appear to contain price data")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        logger.info(f"✓ OHLCV data retrieved successfully ({len(result)} characters)")
        logger.info(f"Result preview:\n{result[:500]}")
        return True
    
    def test_indicator_data_fallback(self, toolkit: Toolkit) -> bool:
        """Test technical indicator retrieval with fallback logic."""
        logger.info(f"Testing RSI indicator for {self.test_symbol}")
        
        result = toolkit.get_indicator_data(
            symbol=self.test_symbol,
            indicator="rsi",
            start_date=self.test_start_date,
            end_date=self.test_end_date,
            interval="1d"
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for indicator data
        if "rsi" not in result.lower():
            logger.error("Result doesn't appear to contain RSI data")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        logger.info(f"✓ Indicator data retrieved successfully ({len(result)} characters)")
        logger.info(f"Result preview:\n{result[:500]}")
        return True
    
    def test_company_news_aggregation(self, toolkit: Toolkit) -> bool:
        """Test company news aggregation from multiple providers."""
        logger.info(f"Testing company news aggregation for {self.test_symbol}")
        
        result = toolkit.get_company_news(
            symbol=self.test_symbol,
            end_date=self.test_end_date,
            lookback_days=self.test_lookback_days
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for provider attribution (should have ## headers)
        if "##" not in result:
            logger.error("Result doesn't contain provider attribution headers")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        # Count number of provider sections
        provider_count = result.count("##")
        logger.info(f"✓ News aggregated from {provider_count} provider(s)")
        logger.info(f"Result preview:\n{result[:1000]}")
        return True
    
    def test_global_news_aggregation(self, toolkit: Toolkit) -> bool:
        """Test global news aggregation from multiple providers."""
        logger.info("Testing global news aggregation")
        
        result = toolkit.get_global_news(
            end_date=self.test_end_date,
            lookback_days=self.test_lookback_days
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for provider attribution
        if "##" not in result:
            logger.error("Result doesn't contain provider attribution headers")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        provider_count = result.count("##")
        logger.info(f"✓ Global news aggregated from {provider_count} provider(s)")
        logger.info(f"Result preview:\n{result[:1000]}")
        return True
    
    def test_insider_transactions_aggregation(self, toolkit: Toolkit) -> bool:
        """Test insider transactions aggregation from multiple providers."""
        logger.info(f"Testing insider transactions for {self.test_symbol}")
        
        result = toolkit.get_insider_transactions(
            symbol=self.test_symbol,
            end_date=self.test_end_date,
            lookback_days=90
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for provider attribution
        if "##" not in result:
            logger.error("Result doesn't contain provider attribution headers")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        provider_count = result.count("##")
        logger.info(f"✓ Insider transactions aggregated from {provider_count} provider(s)")
        logger.info(f"Result preview:\n{result[:1000]}")
        return True
    
    def test_insider_sentiment_aggregation(self, toolkit: Toolkit) -> bool:
        """Test insider sentiment aggregation from multiple providers."""
        logger.info(f"Testing insider sentiment for {self.test_symbol}")
        
        result = toolkit.get_insider_sentiment(
            symbol=self.test_symbol,
            end_date=self.test_end_date,
            lookback_days=90
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for provider attribution
        if "##" not in result:
            logger.error("Result doesn't contain provider attribution headers")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        provider_count = result.count("##")
        logger.info(f"✓ Insider sentiment aggregated from {provider_count} provider(s)")
        logger.info(f"Result preview:\n{result[:1000]}")
        return True
    
    def test_balance_sheet_aggregation(self, toolkit: Toolkit) -> bool:
        """Test balance sheet aggregation from multiple providers."""
        logger.info(f"Testing balance sheet for {self.test_symbol}")
        
        result = toolkit.get_balance_sheet(
            symbol=self.test_symbol,
            frequency="quarterly",
            end_date=self.test_end_date,
            lookback_periods=4
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for provider attribution
        if "##" not in result:
            logger.error("Result doesn't contain provider attribution headers")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        provider_count = result.count("##")
        logger.info(f"✓ Balance sheet aggregated from {provider_count} provider(s)")
        logger.info(f"Result preview:\n{result[:1000]}")
        return True
    
    def test_income_statement_aggregation(self, toolkit: Toolkit) -> bool:
        """Test income statement aggregation from multiple providers."""
        logger.info(f"Testing income statement for {self.test_symbol}")
        
        result = toolkit.get_income_statement(
            symbol=self.test_symbol,
            frequency="quarterly",
            end_date=self.test_end_date,
            lookback_periods=4
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for provider attribution
        if "##" not in result:
            logger.error("Result doesn't contain provider attribution headers")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        provider_count = result.count("##")
        logger.info(f"✓ Income statement aggregated from {provider_count} provider(s)")
        logger.info(f"Result preview:\n{result[:1000]}")
        return True
    
    def test_cashflow_statement_aggregation(self, toolkit: Toolkit) -> bool:
        """Test cash flow statement aggregation from multiple providers."""
        logger.info(f"Testing cash flow statement for {self.test_symbol}")
        
        result = toolkit.get_cashflow_statement(
            symbol=self.test_symbol,
            frequency="quarterly",
            end_date=self.test_end_date,
            lookback_periods=4
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for provider attribution
        if "##" not in result:
            logger.error("Result doesn't contain provider attribution headers")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        provider_count = result.count("##")
        logger.info(f"✓ Cash flow statement aggregated from {provider_count} provider(s)")
        logger.info(f"Result preview:\n{result[:1000]}")
        return True
    
    def test_economic_indicators_aggregation(self, toolkit: Toolkit) -> bool:
        """Test economic indicators aggregation from macro providers."""
        logger.info("Testing economic indicators aggregation")
        
        result = toolkit.get_economic_indicators(
            end_date=self.test_end_date,
            lookback_days=365,
            indicators=["GDP", "UNRATE", "CPIAUCSL"]
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for provider attribution
        if "##" not in result:
            logger.error("Result doesn't contain provider attribution headers")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        provider_count = result.count("##")
        logger.info(f"✓ Economic indicators aggregated from {provider_count} provider(s)")
        logger.info(f"Result preview:\n{result[:1000]}")
        return True
    
    def test_yield_curve_aggregation(self, toolkit: Toolkit) -> bool:
        """Test yield curve aggregation from macro providers."""
        logger.info("Testing yield curve aggregation")
        
        result = toolkit.get_yield_curve(
            end_date=self.test_end_date,
            lookback_days=90
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for provider attribution
        if "##" not in result:
            logger.error("Result doesn't contain provider attribution headers")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        provider_count = result.count("##")
        logger.info(f"✓ Yield curve aggregated from {provider_count} provider(s)")
        logger.info(f"Result preview:\n{result[:1000]}")
        return True
    
    def test_fed_calendar_aggregation(self, toolkit: Toolkit) -> bool:
        """Test Fed calendar aggregation from macro providers."""
        logger.info("Testing Fed calendar aggregation")
        
        result = toolkit.get_fed_calendar(
            end_date=self.test_end_date,
            lookback_days=180
        )
        
        # Validate result
        if not result or len(result) < 10:
            logger.error(f"Result too short: {len(result) if result else 0} characters")
            return False
            
        # Check for provider attribution
        if "##" not in result:
            logger.error("Result doesn't contain provider attribution headers")
            logger.info(f"Result preview: {result[:500]}")
            return False
            
        provider_count = result.count("##")
        logger.info(f"✓ Fed calendar aggregated from {provider_count} provider(s)")
        logger.info(f"Result preview:\n{result[:1000]}")
        return True
    
    def test_error_handling(self, toolkit: Toolkit) -> bool:
        """Test error handling with invalid inputs."""
        logger.info("Testing error handling with invalid symbol")
        
        try:
            # Try with invalid symbol
            result = toolkit.get_ohlcv_data(
                symbol="INVALID_SYMBOL_XYZABC",
                start_date=self.test_start_date,
                end_date=self.test_end_date,
                interval="1d"
            )
            
            # Should either return error message or raise exception
            if "error" in result.lower() or "failed" in result.lower():
                logger.info("✓ Error handling working correctly (error in result)")
                return True
            else:
                logger.error("Expected error message but got successful result")
                return False
                
        except Exception as e:
            logger.info(f"✓ Error handling working correctly (exception raised: {str(e)})")
            return True
    
    def run_all_tests(self):
        """Run all toolkit tests."""
        logger.info("\n" + "="*80)
        logger.info("STARTING COMPREHENSIVE TOOLKIT TESTS")
        logger.info("="*80 + "\n")
        
        # Build provider map
        provider_map = self.build_test_provider_map()
        
        if not provider_map:
            logger.error("❌ Failed to build provider map - no providers available")
            return
        
        # Initialize toolkit
        logger.info("\nInitializing toolkit with provider map...")
        toolkit = Toolkit(provider_map=provider_map)
        logger.info("✓ Toolkit initialized successfully\n")
        
        # Run tests
        self.run_test("OHLCV Data Fallback", lambda: self.test_ohlcv_data_fallback(toolkit))
        self.run_test("Indicator Data Fallback", lambda: self.test_indicator_data_fallback(toolkit))
        self.run_test("Company News Aggregation", lambda: self.test_company_news_aggregation(toolkit))
        self.run_test("Global News Aggregation", lambda: self.test_global_news_aggregation(toolkit))
        self.run_test("Insider Transactions Aggregation", lambda: self.test_insider_transactions_aggregation(toolkit))
        self.run_test("Insider Sentiment Aggregation", lambda: self.test_insider_sentiment_aggregation(toolkit))
        self.run_test("Balance Sheet Aggregation", lambda: self.test_balance_sheet_aggregation(toolkit))
        self.run_test("Income Statement Aggregation", lambda: self.test_income_statement_aggregation(toolkit))
        self.run_test("Cash Flow Statement Aggregation", lambda: self.test_cashflow_statement_aggregation(toolkit))
        self.run_test("Economic Indicators Aggregation", lambda: self.test_economic_indicators_aggregation(toolkit))
        self.run_test("Yield Curve Aggregation", lambda: self.test_yield_curve_aggregation(toolkit))
        self.run_test("Fed Calendar Aggregation", lambda: self.test_fed_calendar_aggregation(toolkit))
        self.run_test("Error Handling", lambda: self.test_error_handling(toolkit))
        
        # Print summary
        self.print_summary()
    
    def print_summary(self):
        """Print test results summary."""
        logger.info("\n" + "="*80)
        logger.info("TEST RESULTS SUMMARY")
        logger.info("="*80)
        
        total_tests = self.results["passed"] + self.results["failed"]
        pass_rate = (self.results["passed"] / total_tests * 100) if total_tests > 0 else 0
        
        logger.info(f"\nTotal Tests: {total_tests}")
        logger.info(f"✅ Passed: {self.results['passed']}")
        logger.info(f"❌ Failed: {self.results['failed']}")
        logger.info(f"Pass Rate: {pass_rate:.1f}%")
        
        if self.results["errors"]:
            logger.info("\nFailed Tests:")
            for error in self.results["errors"]:
                logger.info(f"  - {error}")
        
        logger.info("\n" + "="*80 + "\n")


def main():
    """Main test execution."""
    logger.info("="*80)
    logger.info("TradingAgents New Toolkit - Comprehensive Test Suite")
    logger.info(f"Test Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("="*80)
    
    tester = ToolkitTester()
    tester.run_all_tests()


if __name__ == "__main__":
    main()
