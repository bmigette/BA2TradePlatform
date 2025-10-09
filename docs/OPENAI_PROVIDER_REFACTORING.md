# OpenAI Provider Refactoring

**Date:** October 9, 2025  
**Issue:** OpenAI providers were using get_app_setting for backend_url and model, which should be config values and constructor parameters  
**Solution:** Move backend_url to config.py, make model an optional constructor parameter

## Problem Statement

The OpenAI providers had two configuration issues:

1. **Backend URL from get_app_setting**
   ```python
   # BEFORE (in both providers)
   self.backend_url = get_app_setting("OPENAI_BACKEND_URL", "https://api.openai.com/v1")
   ```
   - Problem: Backend URL is a system-level config, not a user setting
   - Should be in `config.py` with other API endpoints
   - Can be overridden via environment variable

2. **Model from get_app_setting**
   ```python
   # BEFORE (in both providers)
   self.model = get_app_setting("OPENAI_QUICK_THINK_LLM", "gpt-4")
   ```
   - Problem: Model should be passed as a parameter for flexibility
   - Different use cases may need different models
   - Hard-coded in provider makes it inflexible

## Solution Implemented

### 1. Added OPENAI_BACKEND_URL to config.py

**File:** `ba2_trade_platform/config.py`

Added module-level constant:
```python
OPENAI_BACKEND_URL="https://api.openai.com/v1"  # Default OpenAI API endpoint
```

Updated `load_config_from_env()` to load from environment:
```python
def load_config_from_env() -> None:
    global FINNHUB_API_KEY, OPENAI_API_KEY, OPENAI_BACKEND_URL, ALPHA_VANTAGE_API_KEY, ...
    
    OPENAI_BACKEND_URL = os.getenv('OPENAI_BACKEND_URL', OPENAI_BACKEND_URL)
```

**Benefits:**
- System-level configuration (not per-expert)
- Can be overridden via `.env` file
- Consistent with other API endpoint configs
- Available at module import time (no database dependency)

### 2. Updated OpenAINewsProvider

**File:** `ba2_trade_platform/modules/dataproviders/news/OpenAINewsProvider.py`

**Constructor Changes:**
```python
# BEFORE
def __init__(self):
    super().__init__()
    self.backend_url = get_app_setting("OPENAI_BACKEND_URL", "https://api.openai.com/v1")
    self.model = get_app_setting("OPENAI_QUICK_THINK_LLM", "gpt-4")
    self.client = OpenAI(base_url=self.backend_url)
    logger.info("OpenAINewsProvider initialized successfully")

# AFTER
def __init__(self, model: str = None):
    """
    Initialize the OpenAI News Provider.
    
    Args:
        model: OpenAI model to use (e.g., 'gpt-4', 'gpt-4o-mini').
               If not provided, uses OPENAI_QUICK_THINK_LLM from app settings (default: 'gpt-4')
    """
    super().__init__()
    
    # Get OpenAI configuration
    self.backend_url = config.OPENAI_BACKEND_URL
    self.model = model or get_app_setting("OPENAI_QUICK_THINK_LLM", "gpt-4")
    
    self.client = OpenAI(base_url=self.backend_url)
    logger.info(f"OpenAINewsProvider initialized with model={self.model}, backend_url={self.backend_url}")
```

**Key Changes:**
- ✅ `backend_url` from `config.OPENAI_BACKEND_URL` (module constant)
- ✅ `model` is optional constructor parameter with fallback to get_app_setting
- ✅ Better logging showing both model and backend_url

### 3. Updated OpenAICompanyOverviewProvider

**File:** `ba2_trade_platform/modules/dataproviders/fundamentals/overview/OpenAICompanyOverviewProvider.py`

**Constructor Changes:**
```python
# BEFORE
def __init__(self):
    super().__init__()
    self.backend_url = get_app_setting("OPENAI_BACKEND_URL", "https://api.openai.com/v1")
    self.model = get_app_setting("OPENAI_QUICK_THINK_LLM", "gpt-4")
    self.default_lookback_days = int(get_app_setting("ECONOMIC_DATA_DAYS", "90"))
    self.client = OpenAI(base_url=self.backend_url)
    logger.debug("Initialized OpenAICompanyOverviewProvider")

# AFTER
def __init__(self, model: str = None):
    """
    Initialize OpenAI company overview provider.
    
    Args:
        model: OpenAI model to use (e.g., 'gpt-4', 'gpt-4o-mini').
               If not provided, uses OPENAI_QUICK_THINK_LLM from app settings (default: 'gpt-4')
    """
    super().__init__()
    
    # Get OpenAI configuration
    self.backend_url = config.OPENAI_BACKEND_URL
    self.model = model or get_app_setting("OPENAI_QUICK_THINK_LLM", "gpt-4")
    self.default_lookback_days = int(get_app_setting("ECONOMIC_DATA_DAYS", "90"))
    
    self.client = OpenAI(base_url=self.backend_url)
    logger.debug(f"Initialized OpenAICompanyOverviewProvider with model={self.model}, backend_url={self.backend_url}")
```

**Key Changes:**
- ✅ Same pattern as OpenAINewsProvider
- ✅ Consistent parameter handling
- ✅ Better logging

## Usage Patterns

### Current Usage (Backward Compatible)

Since model is optional, existing code continues to work:

```python
# Instantiation without parameters (uses default model from app settings)
provider = OpenAINewsProvider()

# Model will be: get_app_setting("OPENAI_QUICK_THINK_LLM", "gpt-4")
# Backend URL will be: config.OPENAI_BACKEND_URL
```

This is how the new toolkit instantiates providers:
```python
# From agent_utils_new.py
for provider_class in self.provider_map["news"]:
    provider = provider_class()  # ✅ Works - uses default model
```

### Future Usage (With Model Parameter)

Can now pass specific model when needed:

```python
# Use a specific model
fast_provider = OpenAINewsProvider(model="gpt-4o-mini")
deep_provider = OpenAINewsProvider(model="gpt-4")

# Each instance can have different model
```

## Configuration Hierarchy

### Backend URL
1. **Environment Variable** (`.env` file): `OPENAI_BACKEND_URL=https://custom-endpoint.com/v1`
2. **config.py Default**: `https://api.openai.com/v1`

### Model
1. **Constructor Parameter**: `OpenAINewsProvider(model="gpt-4o")`
2. **App Setting**: `OPENAI_QUICK_THINK_LLM` from database
3. **Hard-coded Default**: `"gpt-4"`

## Files Modified

### 1. config.py
- **Line 10**: Added `OPENAI_BACKEND_URL="https://api.openai.com/v1"`
- **Line 18**: Added `OPENAI_BACKEND_URL` to global declaration
- **Line 25**: Added loading from environment: `OPENAI_BACKEND_URL = os.getenv(...)`

### 2. OpenAINewsProvider.py
- **Lines 15-16**: Changed imports to use `from ba2_trade_platform import config` and `from ba2_trade_platform.config import get_app_setting`
- **Lines 29-42**: Updated constructor to accept optional `model` parameter and use `config.OPENAI_BACKEND_URL`

### 3. OpenAICompanyOverviewProvider.py
- **Lines 3-5**: Changed imports to use `from ba2_trade_platform import config`
- **Lines 25-38**: Updated constructor to accept optional `model` parameter and use `config.OPENAI_BACKEND_URL`

## Benefits

### 1. Cleaner Architecture
- System config (backend_url) in config.py ✅
- User preferences (model) can be passed or use defaults ✅
- Separation of concerns between system and user configuration ✅

### 2. Flexibility
- Can instantiate provider with custom model when needed
- Can override backend URL via environment variable
- Maintains backward compatibility with existing code

### 3. Better Logging
- Shows which model is being used
- Shows which backend URL is configured
- Easier debugging

### 4. Consistency
- Matches pattern of other API configurations (FINNHUB_API_KEY, ALPHA_VANTAGE_API_KEY)
- Backend URL treated as infrastructure config, not user setting
- Model parameter allows per-instance configuration when needed

## Testing Recommendations

### 1. Verify Default Behavior
```python
# Test that default instantiation works
provider = OpenAINewsProvider()
assert provider.backend_url == config.OPENAI_BACKEND_URL
assert provider.model == get_app_setting("OPENAI_QUICK_THINK_LLM", "gpt-4")
```

### 2. Verify Custom Model
```python
# Test with custom model
provider = OpenAINewsProvider(model="gpt-4o-mini")
assert provider.model == "gpt-4o-mini"
assert provider.backend_url == config.OPENAI_BACKEND_URL
```

### 3. Verify Environment Override
```bash
# Set in .env
OPENAI_BACKEND_URL=https://custom-endpoint.com/v1
```

```python
# Load config and verify
config.load_config_from_env()
provider = OpenAINewsProvider()
assert provider.backend_url == "https://custom-endpoint.com/v1"
```

### 4. Integration Test
```python
# Run through TradingAgents toolkit
# Verify providers instantiate correctly with no errors
# Check logs for correct model and backend_url values
```

## Migration Notes

### For Existing Code

**No changes required** - existing code continues to work:
```python
provider = OpenAINewsProvider()  # ✅ Still works
```

### For New Code

**Can now specify model** for specialized use cases:
```python
# Use cheaper/faster model for simple tasks
quick_provider = OpenAINewsProvider(model="gpt-4o-mini")

# Use more powerful model for complex analysis
deep_provider = OpenAINewsProvider(model="gpt-4")
```

### For Configuration

**Backend URL configuration** now in standard location:
- **Before**: Set `OPENAI_BACKEND_URL` in app settings (database)
- **After**: Set `OPENAI_BACKEND_URL` in `.env` file or config.py

**Model configuration** more flexible:
- **Before**: Global setting `OPENAI_QUICK_THINK_LLM` for all instances
- **After**: Can use global default OR pass custom model per instance

## Future Enhancements

### 1. Expert-Level Model Configuration
Add model selection to TradingAgents expert settings:

```python
"openai_model": {
    "type": "select",
    "options": ["gpt-4", "gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo"],
    "default": "gpt-4",
    "description": "OpenAI model to use for news analysis"
}
```

Then in toolkit instantiation:
```python
# Get model from expert settings
model = self.settings.get('openai_model', 'gpt-4')

# Pass to provider
if vendor == 'openai':
    provider = provider_class(model=model)
```

### 2. Multiple Model Support
Different models for different tasks:

```python
"openai_quick_model": "gpt-4o-mini",  # For news summaries
"openai_deep_model": "gpt-4",         # For complex analysis
```

### 3. Custom Backend Support
For users with custom OpenAI-compatible endpoints:

```python
# In .env
OPENAI_BACKEND_URL=https://my-custom-llm.company.com/v1
```

Provider automatically uses custom endpoint.

## Conclusion

This refactoring:
- ✅ **Moves backend_url to proper location** (config.py)
- ✅ **Makes model configurable per instance** (optional constructor parameter)
- ✅ **Maintains backward compatibility** (model defaults to app setting)
- ✅ **Improves logging** (shows both model and backend_url)
- ✅ **Enables future enhancements** (expert-level model configuration)

The OpenAI providers now follow best practices for configuration management, with clear separation between system-level config (backend_url) and instance-level config (model).
