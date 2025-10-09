# Expert Alias Feature

**Date:** October 9, 2025  
**Migration:** `7195b1928379_add_alias_to_expertinstance`

## Overview

Added an `alias` field to the `ExpertInstance` model to provide a short, configurable display name for expert instances. This alias is now used throughout the UI in dropdowns, tables, and displays instead of the verbose `user_description` field.

## Changes Made

### 1. Database Model (`ba2_trade_platform/core/models.py`)

Added new field to `ExpertInstance`:
```python
alias: str | None = Field(default=None, max_length=100, description="Short display name for the expert (max 100 chars)")
```

**Key Details:**
- Maximum 100 characters
- Nullable (optional field)
- Separate from `user_description` which remains for detailed notes

### 2. Database Migration

**File:** `alembic/versions/7195b1928379_add_alias_to_expertinstance.py`

- **Upgrade:** Adds `alias` column to `expertinstance` table
- **Downgrade:** Removes `alias` column

**Applied:** ✅ Migration successfully applied to database

### 3. Settings UI (`ba2_trade_platform/ui/pages/settings.py`)

#### Expert Instance Table
- Added "Alias" column to the expert instances table
- Positioned after "Expert Type" column for visibility
- Displays alias value (empty string if not set)

#### Expert Dialog Form
- Added `alias_input` field with:
  - Label: "Alias (max 100 chars)"
  - Placeholder: "Short display name for this expert..."
  - HTML5 `maxlength=100` attribute for client-side validation
- Positioned before the "User Notes" textarea
- Form loads/saves alias value when editing/creating experts

### 4. Display Updates

Updated all locations where expert names are displayed to use `alias` instead of `user_description`:

#### `overview.py` (Line ~1422)
```python
# Before:
base_name = expert_instance.user_description or expert_instance.expert

# After:
base_name = expert_instance.alias or expert_instance.expert
```

#### `marketanalysis.py`
- **Line ~226:** Analysis list table display
- **Line ~894:** Expert dropdown options (simplified to use only alias)
- **Line ~1035:** Scheduled jobs table
- **Line ~1570:** Recommendations table

All now use:
```python
expert_instance.alias or expert_instance.expert
```

#### `market_analysis_detail.py` (Line ~130)
```python
expert_display = expert_instance.alias or expert_instance.expert
ui.label(f'Expert: {expert_display} (ID: {expert_instance.id})')
```

## Usage Pattern

Throughout the application, the display logic is:
```python
display_name = expert_instance.alias or expert_instance.expert
```

This provides:
1. **With Alias:** Shows custom short name (e.g., "My Trading Bot")
2. **Without Alias:** Falls back to expert type (e.g., "TradingAgents")

## Field Comparison

| Field | Purpose | Max Length | Display Usage |
|-------|---------|------------|---------------|
| `expert` | Expert class name (required) | N/A | Fallback if no alias |
| `alias` | Short display name (optional) | 100 chars | Primary display in UI |
| `user_description` | Detailed notes (optional) | Unlimited | Settings table only |

## Benefits

1. **Cleaner UI:** Short, meaningful names in dropdowns instead of long descriptions
2. **Better UX:** Users can quickly identify experts with custom names
3. **Flexibility:** Alias is optional - defaults to expert type name
4. **Separation of Concerns:** Display name (alias) separate from detailed notes (user_description)

## Migration Instructions

The migration has already been applied. For new deployments:

```powershell
# Run migration
$env:PYTHONPATH="."; .venv\Scripts\alembic.exe upgrade head
```

## Testing

To verify the feature works:

1. Navigate to Settings → Experts tab
2. Create or edit an expert instance
3. Enter a short name in the "Alias" field (e.g., "My Trading Bot")
4. Save the expert
5. Verify the alias appears in:
   - Expert settings table
   - Market analysis expert dropdown
   - Analysis list table
   - Recommendations table
   - Order history table

## Backward Compatibility

✅ **Fully backward compatible**
- Existing expert instances without alias will display the expert type name
- `user_description` field remains unchanged and available
- No data loss or migration issues

## Future Considerations

- Consider making alias **required** in a future update for consistency
- Could add validation to prevent duplicate aliases within the same account
- Might add bulk-edit feature to set aliases for multiple experts at once
