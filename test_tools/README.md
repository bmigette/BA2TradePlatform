# Provider Testing Tool

Comprehensive testing utility for all data providers in the BA2 Trade Platform.

## Overview

The `provider_test.py` tool allows you to test all data providers across all categories with optional filtering by category, provider, and method. This is useful for:

- Verifying provider functionality after updates
- Testing API key configurations
- Debugging specific provider methods
- Validating provider integrations
- Performance testing with different providers

## Usage

### Test All Providers

Test every provider in every category:

```bash
python test_tools/provider_test.py
```

### Test Specific Category

Test all providers in a specific category:

```bash
# Test all OHLCV providers
python test_tools/provider_test.py --category ohlcv

# Test all news providers
python test_tools/provider_test.py --category news

# Test all insider trading providers
python test_tools/provider_test.py --category insider
```

### Test Specific Provider

Test a specific provider within a category:

```bash
# Test FMP insider provider
python test_tools/provider_test.py --category insider --provider fmp

# Test AlphaVantage OHLCV provider
python test_tools/provider_test.py --category ohlcv --provider alphavantage

# Test YFinance fundamentals details provider
python test_tools/provider_test.py --category fundamentals_details --provider yfinance
```

### Test Specific Method

Test a single method of a specific provider:

```bash
# Test only get_insider_transactions method
python test_tools/provider_test.py --category insider --provider fmp --method get_insider_transactions

# Test only get_company_overview method
python test_tools/provider_test.py --category fundamentals_overview --provider fmp --method get_company_overview

# Test only get_income_statement method
python test_tools/provider_test.py --category fundamentals_details --provider fmp --method get_income_statement
```

### Verbose Output

Enable detailed logging to see API calls and errors:

```bash
python test_tools/provider_test.py --category news --verbose
```

### List Available Providers

See all available providers organized by category:

```bash
python test_tools/provider_test.py --list
```

## Categories

The tool supports testing the following provider categories:

1. **ohlcv** - Historical stock price data (Open, High, Low, Close, Volume)
2. **indicators** - Technical indicators (RSI, MACD, SMA, etc.)
3. **fundamentals_overview** - Company overview and key metrics
4. **fundamentals_details** - Detailed financial statements (income, balance sheet, cash flow)
5. **news** - Company news articles
6. **macro** - Macroeconomic indicators (GDP, inflation, etc.)
7. **insider** - Insider trading transactions

## Test Methods by Category

### OHLCV Providers
- `get_ohlcv_data_formatted()` - Fetch OHLCV data with formatting

### Indicators Providers
- `get_rsi()` - Relative Strength Index
- `get_macd()` - Moving Average Convergence Divergence
- `get_sma()` - Simple Moving Average

### Fundamentals Overview Providers
- `get_company_overview()` - Company profile and key metrics

### Fundamentals Details Providers
- `get_income_statement()` - Income statement data
- `get_balance_sheet()` - Balance sheet data
- `get_cash_flow()` - Cash flow statement data

### News Providers
- `get_company_news()` - Recent news articles for a symbol

### Macro Providers
- `get_gdp()` - GDP data
- `get_inflation()` - CPI inflation data

### Insider Providers
- `get_insider_transactions()` - Insider trading transactions

## Output Format

### Summary Output

```
============================================================
TEST SUMMARY
============================================================

OHLCV:
  yfinance.get_ohlcv_data_formatted    [PASS] (7 bars)
  alpaca.get_ohlcv_data_formatted      [PASS] (7 bars)
  fmp.get_ohlcv_data_formatted         [PASS] (6 bars)

INSIDER:
  fmp.get_insider_transactions         [PASS] (15 transactions)

============================================================
Total Tests: 4
Passed:      4 (100%)
Failed:      0
Errors:      0
============================================================
```

### Status Codes

- **[PASS]** - Test passed successfully
- **[FAIL]** - Test failed (invalid response format or no data)
- **[ERROR]** - Test encountered an exception

## Configuration Requirements

Some providers require API keys to be configured in the database:

- **AlphaVantage**: Requires `ALPHA_VANTAGE_API_KEY` in AppSetting table
- **Alpaca**: Requires `alpaca_api_key` and `alpaca_api_secret` in AppSetting table
- **FMP**: Requires `FMP_API_KEY` in AppSetting table
- **FRED**: Requires `FRED_API_KEY` in AppSetting table

Set these via the Settings page in the web UI or directly in the database.

## Example Workflows

### After Adding New Provider

Test just the new provider to verify it works:

```bash
# Add FMP OHLCV provider
python test_tools/provider_test.py --category ohlcv --provider fmp --verbose
```

### Before Deployment

Run full test suite to ensure all providers still work:

```bash
python test_tools/provider_test.py > test_results.txt
```

### Debugging API Issues

Test specific method with verbose logging:

```bash
python test_tools/provider_test.py --category news --provider fmp --method get_company_news --verbose
```

### Performance Testing

Compare different providers for the same data type:

```bash
# Test all OHLCV providers to compare performance
python test_tools/provider_test.py --category ohlcv
```

## Extending the Tool

### Adding Tests for New Provider Category

1. Add the provider registry to imports
2. Create a test method (e.g., `test_new_category_provider()`)
3. Add category to `test_category()` registries and test_functions
4. Add category to argparse choices

### Adding Tests for New Methods

Update the `test_methods` dictionary in the corresponding category test method with the new method and test lambda.

## Troubleshooting

### "Provider not found" Error

Make sure:
- Provider is registered in `ba2_trade_platform/modules/dataproviders/__init__.py`
- Category name is correct
- Provider name matches registry key (case-sensitive)

### API Key Errors

Check:
- API key is set in AppSetting table
- Key name matches what provider expects
- API key is valid and has appropriate permissions

### Import Errors

Ensure:
- Virtual environment is activated
- All dependencies are installed: `pip install -r requirements.txt`
- Running from project root: `python test_tools/provider_test.py`

## Related Files

- `ba2_trade_platform/modules/dataproviders/__init__.py` - Provider registries
- `ba2_trade_platform/core/interfaces/` - Provider interface definitions
- `test_fmp_ohlcv.py` - Example of dedicated provider test
- `test_files/test_new_toolkit.py` - Integration tests for TradingAgents toolkit
