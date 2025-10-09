# FMP API Key Naming Consistency Fix

**Date:** October 9, 2025  
**Issue:** Inconsistent API key naming between UI and providers  
**Fix:** Standardized to `FMP_API_KEY` (uppercase)

## Problem Identified

The system had inconsistent naming for the FMP (Financial Modeling Prep) API key:

### Providers (Correct Usage)
**Files:** 
- `FMPNewsProvider.py`
- `FMPInsiderProvider.py`
- `FMPCompanyDetailsProvider.py`

**Code:**
```python
self.api_key = get_app_setting("FMP_API_KEY")  # Uppercase
```

### UI Settings Page (Incorrect Usage)
**File:** `settings.py`

**Code (BEFORE FIX):**
```python
# Loading
fmp = session.exec(select(AppSetting).where(AppSetting.key == 'fmp_api_key')).first()  # Lowercase

# Saving
fmp = AppSetting(key='fmp_api_key', value_str=self.fmp_input.value)  # Lowercase
```

## Impact

This inconsistency meant:
- ❌ **User saves FMP API key via UI** → Saved as `fmp_api_key` (lowercase)
- ❌ **Provider tries to read** → Looks for `FMP_API_KEY` (uppercase)
- ❌ **Result:** Provider can't find the key, throws error: `"FMP API key not configured"`

## Solution Implemented

Changed UI settings page to use uppercase `FMP_API_KEY` to match providers.

### Updated Code

**File:** `ba2_trade_platform/ui/pages/settings.py`

**Loading (Line 392):**
```python
# BEFORE
fmp = session.exec(select(AppSetting).where(AppSetting.key == 'fmp_api_key')).first()

# AFTER
fmp = session.exec(select(AppSetting).where(AppSetting.key == 'FMP_API_KEY')).first()
```

**Saving (Lines 485-492):**
```python
# BEFORE
fmp = session.exec(select(AppSetting).where(AppSetting.key == 'fmp_api_key')).first()
if fmp:
    fmp.value_str = self.fmp_input.value
    update_instance(fmp, session)
else:
    fmp = AppSetting(key='fmp_api_key', value_str=self.fmp_input.value)
    add_instance(fmp, session)

# AFTER
fmp = session.exec(select(AppSetting).where(AppSetting.key == 'FMP_API_KEY')).first()
if fmp:
    fmp.value_str = self.fmp_input.value
    update_instance(fmp, session)
else:
    fmp = AppSetting(key='FMP_API_KEY', value_str=self.fmp_input.value)
    add_instance(fmp, session)
```

## Verification of Other API Keys

Checked all other API key settings for consistency:

| API Key | Database Key | Status |
|---------|-------------|--------|
| OpenAI | `openai_api_key` | ✅ Consistent (lowercase) |
| OpenAI Admin | `openai_admin_api_key` | ✅ Consistent (lowercase) |
| Finnhub | `finnhub_api_key` | ✅ Consistent (lowercase) |
| FRED | `fred_api_key` | ✅ Consistent (lowercase) |
| Alpha Vantage | `alpha_vantage_api_key` | ✅ Consistent (lowercase) |
| **FMP** | **`FMP_API_KEY`** | ✅ **NOW FIXED (uppercase)** |
| Alpaca Key | `alpaca_api_key` | ✅ Consistent (lowercase) |
| Alpaca Secret | `alpaca_api_secret` | ✅ Consistent (lowercase) |

**Note:** FMP is the only API key that uses uppercase. This matches the provider implementation and is now consistent across the codebase.

## Migration Required

### For Existing Users

If users previously saved their FMP API key via the UI, it was stored as `fmp_api_key` (lowercase). After this fix, the UI will look for `FMP_API_KEY` (uppercase) and won't find the old entry.

**Options:**

1. **Manual Database Update:**
   ```sql
   UPDATE appsetting SET key = 'FMP_API_KEY' WHERE key = 'fmp_api_key';
   ```

2. **Re-enter via UI:**
   - User goes to Settings → General Settings
   - Re-enters FMP API key
   - System saves as `FMP_API_KEY` (uppercase)

3. **Automatic Migration (Recommended):**
   We could add migration code to automatically rename the old key:

```python
# In main.py or a migration script
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AppSetting
from sqlmodel import Session, select

def migrate_fmp_api_key():
    """Migrate old lowercase fmp_api_key to uppercase FMP_API_KEY"""
    engine = get_db()
    with Session(engine.bind) as session:
        old_key = session.exec(
            select(AppSetting).where(AppSetting.key == 'fmp_api_key')
        ).first()
        
        if old_key:
            # Check if new key already exists
            new_key = session.exec(
                select(AppSetting).where(AppSetting.key == 'FMP_API_KEY')
            ).first()
            
            if not new_key:
                # Create new key with same value
                new_key = AppSetting(key='FMP_API_KEY', value_str=old_key.value_str)
                session.add(new_key)
                session.commit()
                logger.info("Migrated fmp_api_key to FMP_API_KEY")
            
            # Delete old key
            session.delete(old_key)
            session.commit()
            logger.info("Removed old fmp_api_key entry")
```

## Files Modified

1. **`ba2_trade_platform/ui/pages/settings.py`** (2 changes)
   - Line 392: Load FMP key using `FMP_API_KEY`
   - Lines 485-492: Save FMP key using `FMP_API_KEY`

## Testing Recommendations

### 1. Test UI Save/Load
```python
# Save FMP API key via UI
# Navigate to Settings → General Settings
# Enter FMP API key
# Click Save
# Verify no errors

# Refresh page
# Verify FMP API key is displayed correctly
```

### 2. Test Provider Access
```python
# After saving via UI, try to use FMP provider
from ba2_trade_platform.modules.dataproviders.news import FMPNewsProvider

provider = FMPNewsProvider()  # Should not raise ValueError
# Verify api_key is populated
assert provider.api_key is not None
```

### 3. Test Migration (if implemented)
```python
# Manually create old lowercase key
# Run migration function
# Verify new uppercase key exists
# Verify old lowercase key is deleted
```

## Root Cause

The inconsistency likely originated from:
1. Providers were implemented first using `FMP_API_KEY` (perhaps following environment variable naming convention)
2. UI was added later and developer used lowercase `fmp_api_key` (following pattern of other API keys)
3. No one noticed because testing may have used direct database insertion or environment variables

## Prevention

To prevent similar issues in the future:

### 1. Standardize Naming Convention
- All API keys use lowercase with underscores: `provider_api_key`
- Exception: FMP uses uppercase `FMP_API_KEY` (already established in providers)

### 2. Centralize Key Definitions
Create a constants file:

```python
# ba2_trade_platform/core/constants.py

class AppSettingKeys:
    """Centralized AppSetting key names"""
    OPENAI_API_KEY = "openai_api_key"
    FINNHUB_API_KEY = "finnhub_api_key"
    FRED_API_KEY = "fred_api_key"
    ALPHA_VANTAGE_API_KEY = "alpha_vantage_api_key"
    FMP_API_KEY = "FMP_API_KEY"  # Uppercase per provider convention
    ALPACA_API_KEY = "alpaca_api_key"
    ALPACA_API_SECRET = "alpaca_api_secret"
```

Then use:
```python
# In providers
self.api_key = get_app_setting(AppSettingKeys.FMP_API_KEY)

# In UI
fmp = session.exec(select(AppSetting).where(AppSetting.key == AppSettingKeys.FMP_API_KEY)).first()
```

### 3. Add Integration Tests
```python
def test_api_key_consistency():
    """Verify UI and providers use same key names"""
    # Save via UI
    # Read via provider
    # Verify values match
```

## Conclusion

The FMP API key naming inconsistency has been fixed by standardizing to `FMP_API_KEY` (uppercase) across both UI and providers. This ensures users can successfully save their API key via the UI and have it recognized by the FMP providers.

**Migration needed** for existing users who previously saved FMP API key via UI.
