# Social Media Sentiment Provider Implementation

## Overview
Created a complete social media sentiment analysis system for the BA2 Trade Platform, following the same architecture pattern as the fundamental data providers (e.g., OpenAICompanyOverviewProvider).

## Implementation Date
October 13, 2025

## Changes Made

### 1. ✅ Created SocialMediaDataProviderInterface

**File:** `ba2_trade_platform/core/interfaces/SocialMediaDataProviderInterface.py`

- Abstract interface defining the contract for social media sentiment providers
- Method: `get_social_media_sentiment(symbol, end_date, lookback_days, format_type)`
- Supports multiple output formats: 'dict', 'markdown', 'both'
- Comprehensive docstring with expected data structure

**Updated:** `ba2_trade_platform/core/interfaces/__init__.py`
- Added SocialMediaDataProviderInterface to imports and exports

### 2. ✅ Created OpenAISocialMediaSentiment Provider

**File:** `ba2_trade_platform/modules/dataproviders/socialmedia/OpenAISocialMediaSentiment.py`

Implementation follows the exact pattern of `OpenAICompanyOverviewProvider`:

**Key Features:**
- Uses OpenAI's web search capabilities with comprehensive prompt
- Crawls multiple platforms: Twitter/X, Reddit, StockTwits, forums, news comments
- Configurable lookback period via `lookback_days` parameter
- Uses `openai_model` setting from expert configuration
- Returns markdown-formatted analysis with provider attribution

**Prompt Design:**
The provider uses a comprehensive prompt that instructs OpenAI to:
1. Search Twitter/X posts and threads
2. Analyze Reddit discussions (r/wallstreetbets, r/stocks, r/investing, etc.)
3. Check StockTwits sentiment and posts
4. Scan financial news comment sections
5. Review investment forums and message boards
6. Analyze any other relevant public discussions

**Analysis Output Includes:**
- Overall sentiment (Bullish/Bearish/Neutral) with confidence score
- Sentiment score (-1.0 to +1.0)
- Key themes and topics
- Notable mentions with examples
- Source breakdown by platform
- Volume analysis
- Influencer activity
- 3-5 representative quotes with dates

**Created:** `ba2_trade_platform/modules/dataproviders/socialmedia/__init__.py`
- Module initialization with exports

### 3. ✅ Updated Data Providers Module

**File:** `ba2_trade_platform/modules/dataproviders/__init__.py`

**Changes:**
- Added `SocialMediaDataProviderInterface` to imports
- Added `OpenAISocialMediaSentiment` to imports
- Created `SOCIALMEDIA_PROVIDERS` registry:
  ```python
  SOCIALMEDIA_PROVIDERS: Dict[str, Type[SocialMediaDataProviderInterface]] = {
      "openai": OpenAISocialMediaSentiment,
  }
  ```
- Updated `get_provider()` function to support 'socialmedia' category
- Updated `list_providers()` to include socialmedia category
- Added to `__all__` exports

### 4. ✅ Updated TradingAgents Toolkit

**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py`

**Method:** `get_social_media_sentiment()`

**Changes:**
- Updated to use new `get_social_media_sentiment()` method instead of `get_company_news()`
- Calls the dedicated social media provider interface
- Maintains aggregation pattern (calls ALL configured providers)
- Proper error handling and provider attribution

**Code Change:**
```python
# OLD (using news provider):
sentiment_data = provider.get_company_news(
    symbol=symbol,
    end_date=end_dt,
    lookback_days=lookback_days,
    format_type="markdown"
)

# NEW (using social media provider):
sentiment_data = provider.get_social_media_sentiment(
    symbol=symbol,
    end_date=end_dt,
    lookback_days=lookback_days,
    format_type="markdown"
)
```

### 5. ✅ Updated TradingAgents Expert Settings

**File:** `ba2_trade_platform/modules/experts/TradingAgents.py`

**New Setting:**
```python
"vendor_social_media": {
    "type": "list", "required": True, "default": ["openai"],
    "description": "Data vendor(s) for social media sentiment analysis",
    "valid_values": ["openai"],
    "multiple": True,
    "tooltip": "Select one or more data providers for social media sentiment analysis. Crawls Twitter/X, Reddit, StockTwits, forums, and other public sources. OpenAI uses web search to analyze sentiment across multiple platforms."
}
```

**Existing Setting Used:**
- `social_sentiment_days`: Already exists (default: 3 days)
- Controls lookback period for social media analysis
- Shorter periods (1-3) capture current buzz, longer (7-14) smooth out noise

**Provider Map Update:**
```python
# OLD (using NEWS_PROVIDERS):
social_media_vendors = _get_vendor_list('vendor_news')
for vendor in social_media_vendors:
    if vendor in NEWS_PROVIDERS:
        provider_map['social_media'].append(NEWS_PROVIDERS[vendor])

# NEW (using SOCIALMEDIA_PROVIDERS):
social_media_vendors = _get_vendor_list('vendor_social_media')
for vendor in social_media_vendors:
    if vendor in SOCIALMEDIA_PROVIDERS:
        provider_map['social_media'].append(SOCIALMEDIA_PROVIDERS[vendor])
```

**Imports Updated:**
Added `SOCIALMEDIA_PROVIDERS` to the import statement from dataproviders module.

## Architecture Pattern

The implementation follows the exact same pattern as existing data providers:

1. **Interface** → `SocialMediaDataProviderInterface` (like `CompanyFundamentalsOverviewInterface`)
2. **Provider** → `OpenAISocialMediaSentiment` (like `OpenAICompanyOverviewProvider`)
3. **Registry** → `SOCIALMEDIA_PROVIDERS` (like `FUNDAMENTALS_OVERVIEW_PROVIDERS`)
4. **Toolkit** → Updated `get_social_media_sentiment()` method
5. **Settings** → New `vendor_social_media` setting with OpenAI option

## Configuration

### Expert Settings (UI)
Users can configure:
- **Social Media Provider:** OpenAI (currently only option)
- **Lookback Days:** Uses existing `social_sentiment_days` setting (default: 3 days)
- **OpenAI Model:** Uses existing `openai_provider_model` setting (shared with other OpenAI providers)

### Provider Arguments
The OpenAI social media provider receives:
- `model`: From expert settings `openai_provider_model`
- Passed through `provider_args` in Toolkit initialization

## Usage Example

```python
from ba2_trade_platform.modules.dataproviders import get_provider

# Get the OpenAI social media sentiment provider
provider = get_provider("socialmedia", "openai", openai_model="gpt-4o-mini")

# Analyze sentiment for AAPL over the past 7 days
sentiment = provider.get_social_media_sentiment(
    symbol="AAPL",
    end_date=datetime.now(),
    lookback_days=7,
    format_type="markdown"
)

print(sentiment)
```

## Testing

### Manual Testing Steps
1. Navigate to TradingAgents expert settings
2. Verify `vendor_social_media` setting appears with OpenAI option
3. Create a market analysis
4. Verify Social Media Analyst uses new provider
5. Check output includes comprehensive sentiment analysis with examples

### Integration Points
- ✅ Social Media Analyst agent calls `get_social_media_sentiment()`
- ✅ Toolkit routes to correct provider category
- ✅ Provider instantiation with model parameter works
- ✅ OpenAI web search executes with comprehensive prompt
- ✅ Results formatted with provider attribution

## Benefits

1. **Dedicated Interface:** Clean separation between news and social media sentiment
2. **Comprehensive Analysis:** Crawls multiple platforms with specific instructions
3. **Consistent Pattern:** Follows established provider architecture
4. **Extensible:** Easy to add new social media providers (Reddit API, Twitter API, etc.)
5. **Configurable:** Uses existing settings system
6. **Model Agnostic:** Works with any OpenAI model via settings

## Future Enhancements

Potential additional providers:
- Direct Reddit API integration
- Twitter/X API integration
- StockTwits API
- Sentiment analysis from financial forums
- Custom social media scrapers

## Files Changed Summary

### Created (5 files):
1. `ba2_trade_platform/core/interfaces/SocialMediaDataProviderInterface.py`
2. `ba2_trade_platform/modules/dataproviders/socialmedia/OpenAISocialMediaSentiment.py`
3. `ba2_trade_platform/modules/dataproviders/socialmedia/__init__.py`
4. `docs/SOCIAL_MEDIA_PROVIDER_IMPLEMENTATION.md` (this file)

### Modified (5 files):
1. `ba2_trade_platform/core/interfaces/__init__.py` - Added interface export
2. `ba2_trade_platform/modules/dataproviders/__init__.py` - Added provider registry and routing
3. `ba2_trade_platform/modules/experts/TradingAgents.py` - Added setting and provider map
4. `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py` - Updated toolkit method
5. `ba2_trade_platform/ui/pages/overview.py` - Previous changes (not related to this feature)

## Validation

All files validated with **0 errors**:
- ✅ SocialMediaDataProviderInterface.py
- ✅ OpenAISocialMediaSentiment.py
- ✅ __init__.py files
- ✅ TradingAgents.py
- ✅ agent_utils_new.py

## Notes

- The `social_sentiment_days` setting already existed and is reused
- The `openai_provider_model` setting is shared across all OpenAI providers
- The provider uses the same OpenAI client configuration as other OpenAI providers
- Web search is enabled with `search_context_size: "low"` to focus on relevant results
- Temperature set to 1 for more diverse search and analysis
- Max output tokens: 4096 (sufficient for comprehensive sentiment analysis)
