# AI Data Provider Refactoring

## Overview
Renamed OpenAI data providers to AI providers to support both OpenAI (direct) and NagaAI models with automatic API selection based on model string format.

**Date:** 2025-10-20  
**Branch:** dev  
**Status:** ✅ Complete

## Changes Made

### 1. New Provider Files Created

#### AINewsProvider.py
- **Location:** `ba2_trade_platform/modules/dataproviders/news/AINewsProvider.py`
- **Description:** News provider with dual API support
- **Features:**
  - Supports OpenAI Responses API (for `OpenAI/` prefixed models)
  - Supports NagaAI Chat Completions API with `web_search_options` (for `NagaAI/` and other models)
  - Automatic API selection based on model string format
  - Backward compatible with legacy format (no prefix = OpenAI)

#### AICompanyOverviewProvider.py
- **Location:** `ba2_trade_platform/modules/dataproviders/fundamentals/overview/AICompanyOverviewProvider.py`
- **Description:** Company fundamentals overview provider with dual API support
- **Features:**
  - Same dual API support as AINewsProvider
  - Fetches company metrics (P/E, P/S, EPS, market cap, etc.)

#### AISocialMediaSentiment.py
- **Location:** `ba2_trade_platform/modules/dataproviders/socialmedia/AISocialMediaSentiment.py`
- **Description:** Social media sentiment analysis provider with dual API support
- **Features:**
  - Same dual API support as AINewsProvider
  - Crawls Twitter/X, Reddit, StockTwits, forums, etc.

### 2. Model String Format

**New Format:** `Provider/ModelName`

Examples:
- `OpenAI/gpt-5` - Uses OpenAI Responses API directly
- `OpenAI/gpt-4o-mini` - Uses OpenAI Responses API directly
- `NagaAI/grok-4-fast-reasoning` - Uses NagaAI Chat Completions API
- `NagaAI/gemini-2.5-flash` - Uses NagaAI Chat Completions API
- `gpt-4o` (legacy format) - Defaults to OpenAI Responses API

### 3. API Selection Logic

```python
if self.provider == 'OpenAI':
    # OpenAI models use Responses API directly from OpenAI
    self.api_type = 'responses'
    self.backend_url = config.OPENAI_BACKEND_URL
    self.api_key = get_app_setting('openai_api_key')
elif self.provider == 'NagaAI':
    # NagaAI uses Chat Completions API with web_search_options
    self.api_type = 'chat'
    self.backend_url = 'https://api.naga.ac/v1'
    self.api_key = get_app_setting('naga_ai_api_key')
```

### 4. Updated Import Files

**Modified Files:**
- `ba2_trade_platform/modules/dataproviders/news/__init__.py`
- `ba2_trade_platform/modules/dataproviders/fundamentals/__init__.py`
- `ba2_trade_platform/modules/dataproviders/fundamentals/overview/__init__.py`
- `ba2_trade_platform/modules/dataproviders/socialmedia/__init__.py`
- `ba2_trade_platform/modules/dataproviders/__init__.py` (main)

**Changes:**
- Added imports for new AI providers
- Kept legacy OpenAI provider imports with deprecation comments
- Updated `__all__` exports to include new classes

### 5. Updated Provider Registries

**File:** `ba2_trade_platform/modules/dataproviders/__init__.py`

**Changes:**
```python
FUNDAMENTALS_OVERVIEW_PROVIDERS = {
    "alphavantage": AlphaVantageCompanyOverviewProvider,
    "ai": AICompanyOverviewProvider,  # NEW
    "openai": OpenAICompanyOverviewProvider,  # Legacy - deprecated
    "fmp": FMPCompanyOverviewProvider,
}

NEWS_PROVIDERS = {
    "alpaca": AlpacaNewsProvider,
    "alphavantage": AlphaVantageNewsProvider,
    "google": GoogleNewsProvider,
    "ai": AINewsProvider,  # NEW
    "openai": OpenAINewsProvider,  # Legacy - deprecated
    "fmp": FMPNewsProvider,
    "finnhub": FinnhubNewsProvider,
}

SOCIALMEDIA_PROVIDERS = {
    "ai": AISocialMediaSentiment,  # NEW
    "openai": OpenAISocialMediaSentiment,  # Legacy - deprecated
}
```

### 6. Updated TradingAgents Settings

**File:** `ba2_trade_platform/modules/experts/TradingAgents.py`

**Updated Settings:**

#### vendor_fundamentals
- **Old:** `["alpha_vantage", "openai", "fmp"]`
- **New:** `["alpha_vantage", "ai", "fmp"]`
- **Default:** `["alpha_vantage"]` (unchanged)
- **Tooltip:** Updated to mention "AI uses OpenAI (direct) or NagaAI models"

#### vendor_news
- **Old:** `["openai", "alpaca", "alpha_vantage", "fmp", "finnhub"]`
- **New:** `["ai", "alpaca", "alpha_vantage", "fmp", "finnhub"]`
- **Default:** `["ai", "alpaca"]`
- **Tooltip:** Updated to mention AI provider with dual model support

#### vendor_global_news
- **Old:** `["openai", "fmp", "finnhub"]`
- **New:** `["ai", "fmp", "finnhub"]`
- **Default:** `["ai"]`
- **Tooltip:** Updated to mention AI provider capabilities

#### vendor_social_media
- **Old:** `["openai"]`
- **New:** `["ai"]`
- **Default:** `["ai"]`
- **Tooltip:** Updated to mention AI provider capabilities

## Backward Compatibility

### Legacy Support
All legacy OpenAI provider classes are still available with deprecation comments:
- `OpenAINewsProvider` (use `AINewsProvider` instead)
- `OpenAICompanyOverviewProvider` (use `AICompanyOverviewProvider` instead)
- `OpenAISocialMediaSentiment` (use `AISocialMediaSentiment` instead)

The old `"openai"` registry key still works but is marked as deprecated.

### Migration Path
Existing configurations using `"openai"` will continue to work but should be updated to use `"ai"` for:
1. Better semantic clarity (not just OpenAI anymore)
2. Access to NagaAI models (Grok, Gemini, DeepSeek, etc.)
3. Future-proofing as we may deprecate legacy providers

## NagaAI Web Search API

### Documentation Reference
https://docs.naga.ac/features/web-search

### Key Differences from OpenAI Responses API

**OpenAI Responses API:**
```python
response = client.responses.create(
    model="gpt-5",
    input=[{"role": "system", "content": [{"type": "input_text", "text": prompt}]}],
    tools=[{"type": "web_search_preview", "user_location": {"type": "approximate"}}],
    ...
)
```

**NagaAI Chat Completions API:**
```python
response = client.chat.completions.create(
    model="grok-4-fast-reasoning",
    messages=[{"role": "user", "content": prompt}],
    web_search_options={},  # Enable web search
    ...
)
```

### Supported Models

**OpenAI Models (via OpenAI Responses API):**
- gpt-4o, gpt-4o-mini
- gpt-5, gpt-5-mini, gpt-5-nano
- gpt-4.1, gpt-4.1-mini, gpt-4.1-nano
- o1, o1-mini, o3-mini, o4-mini, o4-mini-deep-research

**NagaAI Models (via NagaAI Chat Completions API):**
- Grok: grok-4-0709, grok-4-fast-reasoning, grok-3, grok-3-mini
- Gemini: gemini-2.5-flash, gemini-2.5-pro, gemini-2.0-flash-001
- DeepSeek: deepseek-v3.2-exp, deepseek-chat-v3.1, deepseek-reasoner-0528
- Plus all GPT models via NagaAI aggregation

## Testing Recommendations

### Test Cases
1. **OpenAI Direct Models:**
   - Set `dataprovider_websearch_model` to `OpenAI/gpt-5`
   - Verify Responses API is used
   - Check web search results are extracted correctly

2. **NagaAI Models:**
   - Set `dataprovider_websearch_model` to `NagaAI/grok-4-fast-reasoning`
   - Verify Chat Completions API with `web_search_options` is used
   - Check citations are included (for Grok models)

3. **Legacy Format:**
   - Set model to `gpt-4o` (no prefix)
   - Verify it defaults to OpenAI Responses API
   - Confirm backward compatibility

4. **Provider Selection:**
   - Set `vendor_news` to `["ai"]`
   - Verify AINewsProvider is instantiated
   - Check model string is passed correctly

### Manual Testing Steps
1. Update expert settings to use `ai` provider instead of `openai`
2. Configure `dataprovider_websearch_model` with various model formats
3. Run market analysis and check logs for API calls
4. Verify news, fundamentals, and social media data is retrieved correctly
5. Test with both OpenAI and NagaAI models

## Files Modified

### New Files (Created)
- `ba2_trade_platform/modules/dataproviders/news/AINewsProvider.py`
- `ba2_trade_platform/modules/dataproviders/fundamentals/overview/AICompanyOverviewProvider.py`
- `ba2_trade_platform/modules/dataproviders/socialmedia/AISocialMediaSentiment.py`

### Modified Files
- `ba2_trade_platform/modules/dataproviders/news/__init__.py`
- `ba2_trade_platform/modules/dataproviders/fundamentals/__init__.py`
- `ba2_trade_platform/modules/dataproviders/fundamentals/overview/__init__.py`
- `ba2_trade_platform/modules/dataproviders/socialmedia/__init__.py`
- `ba2_trade_platform/modules/dataproviders/__init__.py`
- `ba2_trade_platform/modules/experts/TradingAgents.py`

### Legacy Files (Deprecated but Retained)
- `ba2_trade_platform/modules/dataproviders/news/OpenAINewsProvider.py`
- `ba2_trade_platform/modules/dataproviders/fundamentals/overview/OpenAICompanyOverviewProvider.py`
- `ba2_trade_platform/modules/dataproviders/socialmedia/OpenAISocialMediaSentiment.py`

## Next Steps

### Recommended Actions
1. ✅ Update existing database configurations from `"openai"` to `"ai"` in expert settings
2. ✅ Test with various NagaAI models (Grok, Gemini, DeepSeek)
3. ✅ Monitor API costs and performance differences between OpenAI and NagaAI
4. ⏳ Consider removing legacy OpenAI providers after successful migration
5. ⏳ Add comprehensive unit tests for both API types
6. ⏳ Document API key requirements for NagaAI in user documentation

### Future Enhancements
- Add support for Sonar models (always-on web search)
- Implement citation extraction for Grok models (`return_citations: true`)
- Add domain filtering for OpenAI models (`filters.allowed_domains`)
- Support user location configuration (`user_location` parameter)
- Add rate limiting and retry logic for both APIs

## Benefits

### For Users
1. **Broader Model Selection:** Access to Grok, Gemini, DeepSeek via NagaAI
2. **Cost Optimization:** NagaAI offers free tiers for many models
3. **Performance:** Grok models excel at real-time search with X/Twitter integration
4. **Flexibility:** Easy switching between OpenAI direct and NagaAI aggregated models

### For Developers
1. **Unified Interface:** Same provider class works for multiple model sources
2. **Automatic API Selection:** No need to manually choose API type
3. **Backward Compatible:** Existing code continues to work
4. **Future-Proof:** Easy to add new model providers

## Related Documentation

- [NagaAI Web Search Documentation](https://docs.naga.ac/features/web-search)
- [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
- [Data Provider Architecture](./DATA_PROVIDER_REFACTORING_PHASE1.md)
- [Copilot Instructions](../.github/copilot-instructions.md)
