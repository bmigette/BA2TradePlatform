# Expert Settings Import/Export Feature

## Overview
Added import/export functionality for expert settings in the expert configuration dialog. Users can now export expert settings to JSON format and import them back to quickly replicate configurations across different expert instances.

## Changes Made

### 1. OpenAI API Error Logging Fix (ba2_trade_platform/ui/pages/overview.py)

**Issue**: API error logs were using `exc_info=True` outside of exception handlers, causing "NoneType: None" messages.

**Lines Changed**:
- Line 1032: Removed `exc_info=True` from OpenAI API error log (async version)
- Line 1183: Removed `exc_info=True` from OpenAI API error log (sync version)

**Details**:
```python
# Before
logger.error(f'OpenAI API error {response.status}: {error_text}', exc_info=True)

# After  
logger.error(f'OpenAI API error {response.status}: {error_text}')
```

This fixes the issue where the logger was trying to access exception info when none was available, causing the "NoneType: None" error message.

### 2. Expert Settings Import/Export Tab (ba2_trade_platform/ui/pages/settings.py)

#### Tab Addition
- **Line 1231**: Added "Import/Export" tab to the expert dialog tabs
  ```python
  ui.tab('Import/Export', icon='download')
  ```

#### Tab Panel Implementation
- **Lines 1416-1418**: Added import/export tab panel before cleanup tab
  ```python
  with ui.tab_panel('Import/Export'):
      self._render_import_export_tab(expert_instance)
  ```

#### Export Functionality
**Features**:
- Checkbox selection for what to export:
  - General Settings: alias, user description, enabled status, virtual equity
  - Expert Settings: All expert-specific configuration
  - Symbol Settings: Enabled instruments/symbols configuration
  - Instruments: Instrument selection and weighting
- Exports to JSON format with timestamp
- Shows export data in UI for copy-paste (memory-based download)
- Logs exported data for debugging

**Implementation**: `_render_import_export_tab()` method, export section
```python
export_general = ui.checkbox('General Settings', value=True)
export_expert = ui.checkbox('Expert Settings', value=True)
export_symbols = ui.checkbox('Symbol Settings', value=True)
export_instruments = ui.checkbox('Instruments', value=True)
```

#### Import Functionality
**Features**:
- Paste JSON data from previously exported configuration
- Import restores selected settings:
  - General Settings: Updates UI fields (alias, description, enabled, virtual_equity)
  - Expert Settings: Staged for import on save
  - Symbol Settings: Staged for import on save
  - Instruments: Directly updates instrument selector
- Validation of JSON format with error handling
- User notification showing import status

**Implementation**: `_render_import_export_tab()` method, import section
```python
import_textarea = ui.textarea(
    placeholder='Paste JSON export data here',
    value=''
).classes('w-full h-40 mb-4')
```

### 3. Import State Management (ba2_trade_platform/ui/pages/settings.py)

#### Initialization
- **Line 1133-1135**: Initialize import attributes in `show_dialog()`
  ```python
  self._imported_expert_settings = None
  self._imported_symbol_settings = None
  ```

#### Save Integration
- **Lines 2883-2904**: Modified `_save_expert_settings()` to apply imported settings
  ```python
  # Apply imported expert settings if available
  if hasattr(self, '_imported_expert_settings') and self._imported_expert_settings:
      for setting_key, setting_value in self._imported_expert_settings.items():
          expert.save_setting(setting_key, setting_value)
      self._imported_expert_settings = None
  
  # Apply imported symbol settings if available
  if hasattr(self, '_imported_symbol_settings') and self._imported_symbol_settings:
      expert.save_setting('enabled_instruments', self._imported_symbol_settings, setting_type="json")
      self._imported_symbol_settings = None
  ```

## Export Data Format

The export JSON includes the following structure (depending on selected checkboxes):

```json
{
  "general": {
    "alias": "TradingAgents Aggressive",
    "user_description": "Aggressive trading strategy",
    "enabled": true,
    "virtual_equity": 100.0
  },
  "expert_settings": {
    "execution_schedule_enter_market": {...},
    "enable_buy": true,
    "enable_sell": true,
    "vendor_fundamentals": ["alpha_vantage", "ai"],
    "vendor_news": ["ai", "alpaca"]
  },
  "symbol_settings": {
    "AAPL": {"weight": 100.0},
    "MSFT": {"weight": 80.0}
  },
  "instruments": {
    "1": {"enabled": true, "weight": 100.0},
    "2": {"enabled": true, "weight": 80.0}
  }
}
```

## Usage Workflow

### Exporting Settings

1. Open expert settings dialog (edit existing expert)
2. Click "Import/Export" tab
3. Check boxes for settings you want to export:
   - ✅ General Settings: Name, description, status
   - ✅ Expert Settings: Expert-specific configurations
   - ✅ Symbol Settings: Enabled instruments/symbols
   - ✅ Instruments: Instrument selections
4. Click "Export Settings"
5. JSON appears in the text area below
6. Click "Copy to Clipboard" or manually copy the JSON
7. Save to file (e.g., `expert_config_backup.json`)

### Importing Settings

1. Create new expert or edit existing one
2. Click "Import/Export" tab
3. Paste previously exported JSON into the text area
4. Click "Import Settings"
5. Settings are loaded into UI fields
6. Click "Save" to apply all changes

### Typical Use Cases

**Duplicate Expert Configuration**
- Export settings from well-performing expert
- Create new expert instance
- Import settings to replicate configuration
- Adjust if needed and save

**Backup/Restore Configuration**
- Export all settings before major changes
- If changes don't work, import previous configuration
- Save to restore settings

**Share Configuration**
- Export settings as JSON
- Share with team members
- They can import into their instances

## Technical Details

### Storage
- Import state stored in instance attributes (`_imported_expert_settings`, `_imported_symbol_settings`)
- Cleared after successful save to prevent re-application
- JSON parsing handled with error reporting

### Supported Expert Settings
- Any setting key in expert.settings dictionary
- Standard keys: `enable_buy`, `enable_sell`, `allow_automated_trade_opening`, etc.
- Expert-specific keys: `vendor_news`, `vendor_fundamentals`, etc.

### Error Handling
- JSON validation with descriptive error messages
- Missing optional fields gracefully ignored
- Settings that fail to import are logged but don't block other settings
- User notifications for success/failure

## Future Enhancements

Possible improvements:
1. File-based download/upload (avoid copy-paste)
2. Export multiple experts in single file
3. Diff viewer to compare configurations
4. Template library for common strategies
5. Version tracking for configurations
6. Schedule/timing templates

## Testing Checklist

- [ ] Export with all checkboxes selected
- [ ] Export with partial selections
- [ ] Import exported data into new expert
- [ ] Import exported data into existing expert
- [ ] Verify general settings restored correctly
- [ ] Verify expert settings restored correctly
- [ ] Verify symbol/instrument settings restored correctly
- [ ] Test invalid JSON handling
- [ ] Test partial data import (some fields missing)
- [ ] Verify data persistence after save
