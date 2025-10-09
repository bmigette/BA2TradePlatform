"""
Provider Testing Tool

Comprehensive testing utility for all data providers in BA2 Trade Platform.
Tests all provider categories and their methods with optional filtering.

Usage:
    # Test all providers
    python test_tools/provider_test.py
    
    # Test specific category
    python test_tools/provider_test.py --category ohlcv
    
    # Test specific provider
    python test_tools/provider_test.py --category insider --provider fmp
    
    # Test specific method
    python test_tools/provider_test.py --category insider --provider fmp --method get_insider_transactions
    
    # Verbose output
    python test_tools/provider_test.py --verbose
"""

import sys
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timedelta
import traceback

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.modules.dataproviders import (
    OHLCV_PROVIDERS,
    INDICATORS_PROVIDERS,
    FUNDAMENTALS_OVERVIEW_PROVIDERS,
    FUNDAMENTALS_DETAILS_PROVIDERS,
    NEWS_PROVIDERS,
    MACRO_PROVIDERS,
    INSIDER_PROVIDERS,
    list_providers
)
from ba2_trade_platform.logger import logger


class ProviderTester:
    """Test harness for data providers."""
    
    def __init__(self, verbose: bool = False, symbol: str = "AAPL"):
        self.verbose = verbose
        self.results = {}
        self.test_symbol = symbol
        self.test_symbols = [symbol, "MSFT"]
        self.test_end_date = datetime.now()
        self.test_start_date = self.test_end_date - timedelta(days=30)
        
    def log(self, message: str, level: str = "INFO"):
        """Log message if verbose mode enabled."""
        if self.verbose:
            print(f"[{level}] {message}")
    
    def test_ohlcv_provider(self, provider_name: str, provider_class: type, method: Optional[str] = None) -> Dict[str, Any]:
        """Test OHLCV provider methods."""
        results = {}
        
        try:
            provider = provider_class()
            self.log(f"Initialized {provider_name} OHLCV provider")
            
            # Define test methods
            test_methods = {
                "get_ohlcv_data_formatted": lambda: provider.get_ohlcv_data_formatted(
                    symbol=self.test_symbol,
                    lookback_days=7,
                    interval="1d",
                    format_type="both"
                ),
            }
            
            # Filter by specific method if requested
            if method:
                if method not in test_methods:
                    return {method: {"status": "SKIP", "error": f"Method '{method}' not found"}}
                test_methods = {method: test_methods[method]}
            
            # Run tests
            for method_name, test_func in test_methods.items():
                try:
                    self.log(f"Testing {provider_name}.{method_name}()")
                    result = test_func()
                    
                    # Validate result
                    if isinstance(result, dict) and "data" in result:
                        data = result["data"]
                        bar_count = len(data.get("data", []))
                        results[method_name] = {
                            "status": "PASS",
                            "bars": bar_count,
                            "symbol": data.get("symbol"),
                            "interval": data.get("interval"),
                            "result_data": data  # Store full data for output
                        }
                        self.log(f"  SUCCESS: Retrieved {bar_count} bars", "SUCCESS")
                        
                        # Print sample data if verbose
                        if self.verbose and bar_count > 0:
                            bars_data = data.get("data", [])
                            print(f"\n  Sample Data (first 5 bars):")
                            print(f"  {'Date':<12} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>12}")
                            print(f"  {'-'*68}")
                            for bar in bars_data[:5]:
                                # Handle different date formats
                                date_val = bar.get("Date") or bar.get("date") or bar.get("timestamp") or ""
                                if isinstance(date_val, str):
                                    date_str = date_val[:10]
                                else:
                                    date_str = str(date_val)[:10] if date_val else "N/A"
                                print(f"  {date_str:<12} ${bar.get('Open', 0):>9.2f} ${bar.get('High', 0):>9.2f} ${bar.get('Low', 0):>9.2f} ${bar.get('Close', 0):>9.2f} {bar.get('Volume', 0):>12,}")
                            print()
                    else:
                        results[method_name] = {
                            "status": "FAIL",
                            "error": "Invalid response format"
                        }
                        
                except Exception as e:
                    results[method_name] = {
                        "status": "ERROR",
                        "error": str(e)
                    }
                    self.log(f"  ERROR: {e}", "ERROR")
                    if self.verbose:
                        traceback.print_exc()
                        
        except Exception as e:
            results["__init__"] = {
                "status": "ERROR",
                "error": f"Failed to initialize: {str(e)}"
            }
            self.log(f"Failed to initialize {provider_name}: {e}", "ERROR")
            if self.verbose:
                traceback.print_exc()
        
        return results
    
    def test_indicators_provider(self, provider_name: str, provider_class: type, method: Optional[str] = None) -> Dict[str, Any]:
        """Test indicators provider methods."""
        results = {}
        
        try:
            # Indicators providers need an OHLCV provider
            from ba2_trade_platform.modules.dataproviders.ohlcv.YFinanceDataProvider import YFinanceDataProvider
            ohlcv_provider = YFinanceDataProvider()
            
            provider = provider_class(ohlcv_provider)
            self.log(f"Initialized {provider_name} indicators provider")
            
            # Define test methods
            test_methods = {
                "get_rsi": lambda: provider.get_indicator(
                    self.test_symbol,
                    "rsi",
                    end_date=self.test_end_date,
                    lookback_days=30,
                    period=14
                ),
                "get_macd": lambda: provider.get_indicator(
                    self.test_symbol,
                    "macd",
                    end_date=self.test_end_date,
                    lookback_days=30
                ),
                "get_sma": lambda: provider.get_indicator(
                    self.test_symbol,
                    "sma",
                    end_date=self.test_end_date,
                    lookback_days=30,
                    period=20
                ),
            }
            
            # Filter by specific method if requested
            if method:
                if method not in test_methods:
                    return {method: {"status": "SKIP", "error": f"Method '{method}' not found"}}
                test_methods = {method: test_methods[method]}
            
            # Run tests
            for method_name, test_func in test_methods.items():
                try:
                    self.log(f"Testing {provider_name}.{method_name}()")
                    result = test_func()
                    
                    # Validate result
                    if result and "values" in result:
                        value_count = len(result["values"])
                        results[method_name] = {
                            "status": "PASS",
                            "values": value_count,
                            "indicator": result.get("indicator")
                        }
                        self.log(f"  SUCCESS: Retrieved {value_count} values", "SUCCESS")
                    else:
                        results[method_name] = {
                            "status": "FAIL",
                            "error": "Invalid response format"
                        }
                        
                except Exception as e:
                    results[method_name] = {
                        "status": "ERROR",
                        "error": str(e)
                    }
                    self.log(f"  ERROR: {e}", "ERROR")
                    if self.verbose:
                        traceback.print_exc()
                        
        except Exception as e:
            results["__init__"] = {
                "status": "ERROR",
                "error": f"Failed to initialize: {str(e)}"
            }
            self.log(f"Failed to initialize {provider_name}: {e}", "ERROR")
            if self.verbose:
                traceback.print_exc()
        
        return results
    
    def test_fundamentals_overview_provider(self, provider_name: str, provider_class: type, method: Optional[str] = None) -> Dict[str, Any]:
        """Test fundamentals overview provider methods."""
        results = {}
        
        try:
            provider = provider_class()
            self.log(f"Initialized {provider_name} fundamentals overview provider")
            
            # Define test methods
            test_methods = {
                "get_fundamentals_overview": lambda: provider.get_fundamentals_overview(
                    self.test_symbol,
                    as_of_date=self.test_end_date,
                    format_type="dict"
                ),
            }
            
            # Filter by specific method if requested
            if method:
                if method not in test_methods:
                    return {method: {"status": "SKIP", "error": f"Method '{method}' not found"}}
                test_methods = {method: test_methods[method]}
            
            # Run tests
            for method_name, test_func in test_methods.items():
                try:
                    self.log(f"Testing {provider_name}.{method_name}()")
                    result = test_func()
                    
                    # Validate result
                    if result and isinstance(result, dict):
                        results[method_name] = {
                            "status": "PASS",
                            "symbol": result.get("symbol"),
                            "company_name": result.get("company_name", result.get("name")),
                            "sector": result.get("metrics", {}).get("sector"),
                            "market_cap": result.get("metrics", {}).get("market_cap"),
                            "result_data": result
                        }
                        company_name = result.get("company_name", result.get("name", "N/A"))
                        self.log(f"  SUCCESS: {company_name}", "SUCCESS")
                        
                        # Print sample metrics if verbose
                        if self.verbose:
                            metrics = result.get("metrics", {})
                            print(f"\n  Company Overview - {company_name}:")
                            print(f"  {'-'*60}")
                            if metrics.get("sector"):
                                print(f"  Sector: {metrics['sector']}")
                            if metrics.get("industry"):
                                print(f"  Industry: {metrics['industry']}")
                            if metrics.get("market_cap"):
                                print(f"  Market Cap: ${metrics['market_cap']:,.0f}")
                            if metrics.get("price"):
                                print(f"  Current Price: ${metrics['price']:.2f}")
                            if metrics.get("beta"):
                                print(f"  Beta: {metrics['beta']:.2f}")
                            if metrics.get("ceo"):
                                print(f"  CEO: {metrics['ceo']}")
                            print()
                    else:
                        results[method_name] = {
                            "status": "FAIL",
                            "error": "Invalid response format or no data"
                        }
                        
                except Exception as e:
                    results[method_name] = {
                        "status": "ERROR",
                        "error": str(e)
                    }
                    self.log(f"  ERROR: {e}", "ERROR")
                    if self.verbose:
                        traceback.print_exc()
                        
        except Exception as e:
            results["__init__"] = {
                "status": "ERROR",
                "error": f"Failed to initialize: {str(e)}"
            }
            self.log(f"Failed to initialize {provider_name}: {e}", "ERROR")
            if self.verbose:
                traceback.print_exc()
        
        return results
    
    def test_fundamentals_details_provider(self, provider_name: str, provider_class: type, method: Optional[str] = None) -> Dict[str, Any]:
        """Test fundamentals details provider methods."""
        results = {}
        
        try:
            provider = provider_class()
            self.log(f"Initialized {provider_name} fundamentals details provider")
            
            # Define test methods
            test_methods = {
                "get_income_statement": lambda: provider.get_income_statement(self.test_symbol, period="annual"),
                "get_balance_sheet": lambda: provider.get_balance_sheet(self.test_symbol, period="annual"),
                "get_cash_flow": lambda: provider.get_cash_flow(self.test_symbol, period="annual"),
            }
            
            # Filter by specific method if requested
            if method:
                if method not in test_methods:
                    return {method: {"status": "SKIP", "error": f"Method '{method}' not found"}}
                test_methods = {method: test_methods[method]}
            
            # Run tests
            for method_name, test_func in test_methods.items():
                try:
                    self.log(f"Testing {provider_name}.{method_name}()")
                    result = test_func()
                    
                    # Validate result
                    if result and isinstance(result, dict):
                        statement_count = len(result.get("statements", []))
                        results[method_name] = {
                            "status": "PASS",
                            "statements": statement_count,
                            "symbol": result.get("symbol")
                        }
                        self.log(f"  SUCCESS: Retrieved {statement_count} statements", "SUCCESS")
                    else:
                        results[method_name] = {
                            "status": "FAIL",
                            "error": "Invalid response format or no data"
                        }
                        
                except Exception as e:
                    results[method_name] = {
                        "status": "ERROR",
                        "error": str(e)
                    }
                    self.log(f"  ERROR: {e}", "ERROR")
                    if self.verbose:
                        traceback.print_exc()
                        
        except Exception as e:
            results["__init__"] = {
                "status": "ERROR",
                "error": f"Failed to initialize: {str(e)}"
            }
            self.log(f"Failed to initialize {provider_name}: {e}", "ERROR")
            if self.verbose:
                traceback.print_exc()
        
        return results
    
    def test_news_provider(self, provider_name: str, provider_class: type, method: Optional[str] = None) -> Dict[str, Any]:
        """Test news provider methods."""
        results = {}
        
        try:
            provider = provider_class()
            self.log(f"Initialized {provider_name} news provider")
            
            # Define test methods
            test_methods = {
                "get_company_news": lambda: provider.get_company_news(
                    self.test_symbol,
                    end_date=self.test_end_date,
                    lookback_days=7,
                    format_type="both"  # Request both dict and markdown
                ),
            }
            
            # Filter by specific method if requested
            if method:
                if method not in test_methods:
                    return {method: {"status": "SKIP", "error": f"Method '{method}' not found"}}
                test_methods = {method: test_methods[method]}
            
            # Run tests
            for method_name, test_func in test_methods.items():
                try:
                    self.log(f"Testing {provider_name}.{method_name}()")
                    result = test_func()
                    
                    # Validate result - handle both dict with "data" key and dict with "articles" key
                    if isinstance(result, dict):
                        # Check if it's the "both" format with "data" key
                        if "data" in result:
                            data = result["data"]
                            if "articles" in data:
                                article_count = len(data["articles"])
                                results[method_name] = {
                                    "status": "PASS",
                                    "articles": article_count,
                                    "symbol": data.get("symbol"),
                                    "result_data": data
                                }
                                self.log(f"  SUCCESS: Retrieved {article_count} articles", "SUCCESS")
                                
                                # Print sample articles if verbose
                                if self.verbose and article_count > 0:
                                    print(f"\n  Sample News Articles (first 3):")
                                    print(f"  {'-'*80}")
                                    for i, article in enumerate(data["articles"][:3], 1):
                                        print(f"  {i}. {article.get('title', 'No title')}")
                                        print(f"     Source: {article.get('source', 'N/A')} | Date: {article.get('published_date', 'N/A')[:10]}")
                                        if article.get('url'):
                                            print(f"     URL: {article.get('url')}")
                                        print()
                            else:
                                results[method_name] = {
                                    "status": "FAIL",
                                    "error": "No articles key in data"
                                }
                        # Check if it's direct dict format
                        elif "articles" in result:
                            article_count = len(result["articles"])
                            results[method_name] = {
                                "status": "PASS",
                                "articles": article_count,
                                "symbol": result.get("symbol"),
                                "result_data": result
                            }
                            self.log(f"  SUCCESS: Retrieved {article_count} articles", "SUCCESS")
                            
                            # Print sample articles if verbose
                            if self.verbose and article_count > 0:
                                print(f"\n  Sample News Articles (first 3):")
                                print(f"  {'-'*80}")
                                for i, article in enumerate(result["articles"][:3], 1):
                                    print(f"  {i}. {article.get('title', 'No title')}")
                                    print(f"     Source: {article.get('source', 'N/A')} | Date: {article.get('published_date', 'N/A')[:10]}")
                                    if article.get('url'):
                                        print(f"     URL: {article.get('url')}")
                                    print()
                        else:
                            results[method_name] = {
                                "status": "FAIL",
                                "error": "Invalid response format - missing articles"
                            }
                    else:
                        results[method_name] = {
                            "status": "FAIL",
                            "error": "Invalid response format - not a dict"
                        }
                        
                except Exception as e:
                    results[method_name] = {
                        "status": "ERROR",
                        "error": str(e)
                    }
                    self.log(f"  ERROR: {e}", "ERROR")
                    if self.verbose:
                        traceback.print_exc()
                        
        except Exception as e:
            results["__init__"] = {
                "status": "ERROR",
                "error": f"Failed to initialize: {str(e)}"
            }
            self.log(f"Failed to initialize {provider_name}: {e}", "ERROR")
            if self.verbose:
                traceback.print_exc()
        
        return results
    
    def test_macro_provider(self, provider_name: str, provider_class: type, method: Optional[str] = None) -> Dict[str, Any]:
        """Test macro economics provider methods."""
        results = {}
        
        try:
            provider = provider_class()
            self.log(f"Initialized {provider_name} macro provider")
            
            # Define test methods
            test_methods = {
                "get_gdp": lambda: provider.get_economic_indicator(
                    "GDP",
                    end_date=self.test_end_date,
                    lookback_days=365
                ),
                "get_inflation": lambda: provider.get_economic_indicator(
                    "CPIAUCSL",  # CPI inflation
                    end_date=self.test_end_date,
                    lookback_days=365
                ),
            }
            
            # Filter by specific method if requested
            if method:
                if method not in test_methods:
                    return {method: {"status": "SKIP", "error": f"Method '{method}' not found"}}
                test_methods = {method: test_methods[method]}
            
            # Run tests
            for method_name, test_func in test_methods.items():
                try:
                    self.log(f"Testing {provider_name}.{method_name}()")
                    result = test_func()
                    
                    # Validate result
                    if result and isinstance(result, dict):
                        value_count = len(result.get("values", []))
                        results[method_name] = {
                            "status": "PASS",
                            "values": value_count,
                            "indicator": result.get("indicator")
                        }
                        self.log(f"  SUCCESS: Retrieved {value_count} values", "SUCCESS")
                    else:
                        results[method_name] = {
                            "status": "FAIL",
                            "error": "Invalid response format or no data"
                        }
                        
                except Exception as e:
                    results[method_name] = {
                        "status": "ERROR",
                        "error": str(e)
                    }
                    self.log(f"  ERROR: {e}", "ERROR")
                    if self.verbose:
                        traceback.print_exc()
                        
        except Exception as e:
            results["__init__"] = {
                "status": "ERROR",
                "error": f"Failed to initialize: {str(e)}"
            }
            self.log(f"Failed to initialize {provider_name}: {e}", "ERROR")
            if self.verbose:
                traceback.print_exc()
        
        return results
    
    def test_insider_provider(self, provider_name: str, provider_class: type, method: Optional[str] = None) -> Dict[str, Any]:
        """Test insider trading provider methods."""
        results = {}
        
        try:
            provider = provider_class()
            self.log(f"Initialized {provider_name} insider provider")
            
            # Define test methods
            test_methods = {
                "get_insider_transactions": lambda: provider.get_insider_transactions(
                    self.test_symbol,
                    end_date=self.test_end_date,
                    lookback_days=90,
                    format_type="both"  # Request both dict and markdown
                ),
            }
            
            # Filter by specific method if requested
            if method:
                if method not in test_methods:
                    return {method: {"status": "SKIP", "error": f"Method '{method}' not found"}}
                test_methods = {method: test_methods[method]}
            
            # Run tests
            for method_name, test_func in test_methods.items():
                try:
                    self.log(f"Testing {provider_name}.{method_name}()")
                    result = test_func()
                    
                    # Validate result - handle both dict with "data" key and dict with "transactions" key
                    if isinstance(result, dict):
                        # Check if it's the "both" format with "data" key
                        if "data" in result:
                            data = result["data"]
                            if "transactions" in data:
                                transaction_count = len(data["transactions"])
                                results[method_name] = {
                                    "status": "PASS",
                                    "transactions": transaction_count,
                                    "symbol": data.get("symbol"),
                                    "result_data": data
                                }
                                self.log(f"  SUCCESS: Retrieved {transaction_count} transactions", "SUCCESS")
                                
                                # Print sample transactions if verbose
                                if self.verbose and transaction_count > 0:
                                    print(f"\n  Sample Insider Transactions (first 5):")
                                    print(f"  {'Date':<12} {'Name':<25} {'Type':<10} {'Shares':>12} {'Price':>10}")
                                    print(f"  {'-'*80}")
                                    for txn in data["transactions"][:5]:
                                        date_str = txn.get("filing_date", "")[:10] if txn.get("filing_date") else "N/A"
                                        name = (txn.get("insider_name", "N/A")[:23] + "..") if len(txn.get("insider_name", "")) > 25 else txn.get("insider_name", "N/A")
                                        txn_type = txn.get("transaction_type", "N/A")[:10]
                                        shares = txn.get("shares", 0)
                                        price = txn.get("price_per_share", 0)
                                        print(f"  {date_str:<12} {name:<25} {txn_type:<10} {shares:>12,} ${price:>9.2f}")
                                    print()
                            else:
                                results[method_name] = {
                                    "status": "FAIL",
                                    "error": "No transactions key in data"
                                }
                        # Check if it's direct dict format
                        elif "transactions" in result:
                            transaction_count = len(result["transactions"])
                            results[method_name] = {
                                "status": "PASS",
                                "transactions": transaction_count,
                                "symbol": result.get("symbol"),
                                "result_data": result
                            }
                            self.log(f"  SUCCESS: Retrieved {transaction_count} transactions", "SUCCESS")
                            
                            # Print sample transactions if verbose
                            if self.verbose and transaction_count > 0:
                                print(f"\n  Sample Insider Transactions (first 5):")
                                print(f"  {'Date':<12} {'Name':<25} {'Type':<10} {'Shares':>12} {'Price':>10}")
                                print(f"  {'-'*80}")
                                for txn in result["transactions"][:5]:
                                    date_str = txn.get("filing_date", "")[:10] if txn.get("filing_date") else "N/A"
                                    name = (txn.get("insider_name", "N/A")[:23] + "..") if len(txn.get("insider_name", "")) > 25 else txn.get("insider_name", "N/A")
                                    txn_type = txn.get("transaction_type", "N/A")[:10]
                                    shares = txn.get("shares", 0)
                                    price = txn.get("price_per_share", 0)
                                    print(f"  {date_str:<12} {name:<25} {txn_type:<10} {shares:>12,} ${price:>9.2f}")
                                print()
                        else:
                            results[method_name] = {
                                "status": "FAIL",
                                "error": "Invalid response format - missing transactions"
                            }
                    else:
                        results[method_name] = {
                            "status": "FAIL",
                            "error": "Invalid response format - not a dict"
                        }
                        
                except Exception as e:
                    results[method_name] = {
                        "status": "ERROR",
                        "error": str(e)
                    }
                    self.log(f"  ERROR: {e}", "ERROR")
                    if self.verbose:
                        traceback.print_exc()
                        
        except Exception as e:
            results["__init__"] = {
                "status": "ERROR",
                "error": f"Failed to initialize: {str(e)}"
            }
            self.log(f"Failed to initialize {provider_name}: {e}", "ERROR")
            if self.verbose:
                traceback.print_exc()
        
        return results
    
    def test_category(self, category: str, provider_name: Optional[str] = None, method: Optional[str] = None) -> Dict[str, Any]:
        """Test all providers in a category."""
        category_results = {}
        
        # Get registry for category
        registries = {
            "ohlcv": OHLCV_PROVIDERS,
            "indicators": INDICATORS_PROVIDERS,
            "fundamentals_overview": FUNDAMENTALS_OVERVIEW_PROVIDERS,
            "fundamentals_details": FUNDAMENTALS_DETAILS_PROVIDERS,
            "news": NEWS_PROVIDERS,
            "macro": MACRO_PROVIDERS,
            "insider": INSIDER_PROVIDERS,
        }
        
        # Get test function for category
        test_functions = {
            "ohlcv": self.test_ohlcv_provider,
            "indicators": self.test_indicators_provider,
            "fundamentals_overview": self.test_fundamentals_overview_provider,
            "fundamentals_details": self.test_fundamentals_details_provider,
            "news": self.test_news_provider,
            "macro": self.test_macro_provider,
            "insider": self.test_insider_provider,
        }
        
        if category not in registries:
            return {"error": f"Unknown category: {category}"}
        
        registry = registries[category]
        test_func = test_functions[category]
        
        # Filter by provider if specified
        if provider_name:
            if provider_name not in registry:
                return {"error": f"Provider '{provider_name}' not found in category '{category}'"}
            providers_to_test = {provider_name: registry[provider_name]}
        else:
            providers_to_test = registry
        
        # Test each provider
        for prov_name, prov_class in providers_to_test.items():
            print(f"\n{'='*60}")
            print(f"Testing {category.upper()} Provider: {prov_name}")
            print(f"{'='*60}")
            
            category_results[prov_name] = test_func(prov_name, prov_class, method)
        
        return category_results
    
    def test_all(self) -> Dict[str, Any]:
        """Test all providers in all categories."""
        all_results = {}
        
        categories = [
            "ohlcv",
            "indicators",
            "fundamentals_overview",
            "fundamentals_details",
            "news",
            "macro",
            "insider"
        ]
        
        for category in categories:
            print(f"\n{'#'*60}")
            print(f"# CATEGORY: {category.upper()}")
            print(f"{'#'*60}")
            
            all_results[category] = self.test_category(category)
        
        return all_results
    
    def print_summary(self, results: Dict[str, Any]):
        """Print test results summary."""
        print(f"\n{'='*60}")
        print("TEST SUMMARY")
        print(f"{'='*60}\n")
        
        total_tests = 0
        passed_tests = 0
        failed_tests = 0
        error_tests = 0
        
        for category, category_results in results.items():
            if "error" in category_results:
                print(f"{category:30} [ERROR] {category_results['error']}")
                continue
            
            print(f"\n{category.upper()}:")
            for provider, provider_results in category_results.items():
                for method, method_result in provider_results.items():
                    total_tests += 1
                    status = method_result.get("status", "UNKNOWN")
                    
                    if status == "PASS":
                        passed_tests += 1
                        status_icon = "[PASS]"
                    elif status == "FAIL":
                        failed_tests += 1
                        status_icon = "[FAIL]"
                    else:
                        error_tests += 1
                        status_icon = "[ERROR]"
                    
                    # Format method info
                    method_info = f"{provider}.{method}"
                    
                    # Add additional info
                    extra_info = []
                    if "bars" in method_result:
                        extra_info.append(f"{method_result['bars']} bars")
                    if "articles" in method_result:
                        extra_info.append(f"{method_result['articles']} articles")
                    if "transactions" in method_result:
                        extra_info.append(f"{method_result['transactions']} transactions")
                    if "statements" in method_result:
                        extra_info.append(f"{method_result['statements']} statements")
                    if "values" in method_result:
                        extra_info.append(f"{method_result['values']} values")
                    if "company_name" in method_result:
                        extra_info.append(f"{method_result['company_name']}")
                    
                    extra_str = f" ({', '.join(extra_info)})" if extra_info else ""
                    
                    # Print result
                    print(f"  {method_info:40} {status_icon}{extra_str}")
                    
                    # Print error if present
                    if "error" in method_result and self.verbose:
                        print(f"    Error: {method_result['error']}")
        
        # Overall summary
        print(f"\n{'='*60}")
        print(f"Total Tests: {total_tests}")
        print(f"Passed:      {passed_tests} ({100*passed_tests/total_tests if total_tests > 0 else 0:.0f}%)")
        print(f"Failed:      {failed_tests}")
        print(f"Errors:      {error_tests}")
        print(f"{'='*60}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test BA2 Trade Platform data providers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test all providers
  python test_tools/provider_test.py
  
  # Test specific category
  python test_tools/provider_test.py --category ohlcv
  
  # Test specific provider
  python test_tools/provider_test.py --category insider --provider fmp
  
  # Test specific method
  python test_tools/provider_test.py --category insider --provider fmp --method get_insider_transactions
  
  # Verbose output
  python test_tools/provider_test.py --category news --verbose
        """
    )
    
    parser.add_argument(
        "--category",
        choices=["ohlcv", "indicators", "fundamentals_overview", "fundamentals_details", "news", "macro", "insider"],
        help="Test specific provider category"
    )
    
    parser.add_argument(
        "--provider",
        help="Test specific provider (requires --category)"
    )
    
    parser.add_argument(
        "--method",
        help="Test specific method (requires --category and --provider)"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output with detailed logging"
    )
    
    parser.add_argument(
        "--symbol", "-s",
        default="AAPL",
        help="Stock symbol to test with (default: AAPL)"
    )
    
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all available providers and exit"
    )
    
    args = parser.parse_args()
    
    # List providers if requested
    if args.list:
        print("\nAvailable Providers:")
        print("="*60)
        all_providers = list_providers()
        for category, providers in all_providers.items():
            print(f"\n{category.upper()}:")
            for provider in providers:
                print(f"  - {provider}")
        return
    
    # Validate arguments
    if args.method and not (args.category and args.provider):
        parser.error("--method requires both --category and --provider")
    
    if args.provider and not args.category:
        parser.error("--provider requires --category")
    
    # Create tester
    tester = ProviderTester(verbose=args.verbose, symbol=args.symbol)
    
    # Run tests
    if args.category:
        results = {args.category: tester.test_category(args.category, args.provider, args.method)}
    else:
        results = tester.test_all()
    
    # Print summary
    tester.print_summary(results)


if __name__ == "__main__":
    main()
