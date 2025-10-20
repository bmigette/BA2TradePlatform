# Allow Custom Values Feature

## Overview
Added `allow_custom` flag to settings definitions that enables users to enter custom values not in the predefined `valid_values` list. This is particularly useful for model names where new models may be added by providers between platform updates.

## Implementation

### 1. Settings Definition
Add `allow_custom: True` to any setting with `valid_values`:

```python
"setting_name": {
    "type": "str",
    "required": True,
    "default": "some-value",
    "valid_values": ["value1", "value2", "value3"],
    "allow_custom": True,  # Enable custom value input
    "description": "...",
    "tooltip": "..."
}
```

### 2. UI Behavior

**Without `allow_custom` (default behavior):**
- Shows regular dropdown (ui.select)
- User can only select from predefined list
- Restricted to values in `valid_values`

**With `allow_custom: True`:**
- Shows editable select dropdown (ui.select with `with_input=True`)
- User can:
  - Select from dropdown list
  - Type custom values directly
  - Custom values are added to the dropdown automatically
- Custom values are validated and saved like any other value

### 3. Example: TradingAgents Model Settings

All three model settings now support custom values:

```python
"deep_think_llm": {
    "type": "str",
    "required": True,
    "default": "OpenAI/gpt-4o-mini",
    "valid_values": [
        "OpenAI/gpt-4o",
        "OpenAI/gpt-4o-mini",
        "NagaAI/deepseek-v3.2-exp:free",
        "NagaAI/gemini-2.5-flash:free",
        # ... more models
    ],
    "allow_custom": True,  # Users can enter any model name
    "tooltip": "... You can also enter custom model names."
}
```

## Use Cases

### 1. New Naga AI Models
When Naga AI adds new models (after adding credits to account):
- User can type the model name directly: `NagaAI/claude-opus-4:paid`
- No need to wait for platform update
- Model name is saved and works immediately

### 2. Beta/Preview Models
For testing preview models:
- Enter: `OpenAI/gpt-5-preview`
- Enter: `NagaAI/experimental-model:beta`
- Useful for early access testing

### 3. Custom API Endpoints
If Naga AI releases regional endpoints:
- Can specify: `NagaAI-EU/gemini-2.5-flash:free`
- Parser can be updated to handle new prefixes

## User Experience

**Dropdown View:**
```
┌─────────────────────────────────────┐
│ Deep Think LLM               ▼      │
├─────────────────────────────────────┤
│ OpenAI/gpt-4o-mini                  │ ← Currently selected
│ OpenAI/gpt-4o                       │
│ NagaAI/deepseek-v3.2-exp:free       │
│ NagaAI/gemini-2.5-flash:free        │
│ ...                                 │
│ [Type to add custom model...]       │ ← Can type here
└─────────────────────────────────────┘
```

**Typing Custom Value:**
```
┌─────────────────────────────────────┐
│ NagaAI/claude-opus-4:paid▌          │ ← User typing
└─────────────────────────────────────┘
```

**After Saving:**
The custom value is saved to the database and can be reused.

## Technical Details

### UI Implementation (settings.py)

```python
# Check for allow_custom flag
allow_custom = meta.get("allow_custom", False)

if valid_values:
    if allow_custom:
        # Editable select
        inp = ui.select(
            options=valid_values,
            label=display_label,
            value=value,
            with_input=True,          # Enable text input
            new_value_mode='add-unique'  # Allow custom values
        ).classes('w-full')
    else:
        # Regular select (restricted)
        inp = ui.select(
            options=valid_values,
            label=display_label,
            value=value if value in valid_values else valid_values[0]
        ).classes('w-full')
```

### NiceGUI Parameters

- `with_input=True`: Enables text input in the select component
- `new_value_mode='add-unique'`: Allows adding new values that aren't duplicates
- Values typed by user are automatically added to options list
- Saving works the same way as selecting from dropdown

## Validation

### Current Behavior
- No additional validation for custom values
- Custom values are saved as-is
- Model parser handles unknown models gracefully (defaults to OpenAI)

### Future Enhancements (Optional)
Could add validation callback:
```python
"allow_custom": True,
"validate_custom": lambda v: v.count('/') == 1,  # Must have Provider/Model format
"validation_error": "Model must be in format: Provider/ModelName"
```

## Benefits

### 1. Future-Proof
- Platform works with new models without code updates
- Users aren't blocked waiting for releases

### 2. Flexibility
- Power users can experiment with beta models
- Testing new providers easy

### 3. Backward Compatible
- Default is `allow_custom=False`
- Existing settings behavior unchanged
- Only affects settings where explicitly enabled

## Settings Updated

### TradingAgents Expert
All three model settings now have `allow_custom=True`:

1. **deep_think_llm**
   - For complex reasoning models
   - Can enter any OpenAI or Naga AI model

2. **quick_think_llm**
   - For fast analysis models
   - Can enter any lightweight model

3. **openai_provider_model**
   - For web search models
   - Can enter any search-capable model

## Example Usage

### Scenario 1: Naga AI Adds New Model
**Problem:** User adds credits, sees new model `NagaAI/gpt-6-turbo:paid` in Naga AI dashboard

**Solution:**
1. Go to TradingAgents settings
2. Click on `deep_think_llm` dropdown
3. Type: `NagaAI/gpt-6-turbo:paid`
4. Press Enter or click away
5. Save expert settings
6. Model is now usable!

### Scenario 2: Testing Beta Model
**Problem:** OpenAI releases `o4-preview` for beta testing

**Solution:**
1. Enter `OpenAI/o4-preview` in model field
2. Parser recognizes OpenAI prefix
3. Uses OpenAI endpoint and API key
4. Works immediately

### Scenario 3: Regional Endpoint (Future)
**Problem:** Need to use EU-specific endpoint

**Solution:**
1. Enter `NagaAI-EU/gemini-2.5-flash:free`
2. Update parser to recognize `NagaAI-EU` prefix
3. Point to `https://eu.api.naga.ac/v1`
4. No UI changes needed

## Testing

### Manual Test
1. Create TradingAgents expert
2. In settings, click on `deep_think_llm`
3. Verify dropdown shows all models
4. Type a custom value: `NagaAI/test-model:free`
5. Press Enter
6. Save expert
7. Reload page
8. Verify custom value is shown and selected

### Expected Behavior
- ✅ Can select from dropdown
- ✅ Can type custom values
- ✅ Custom values are saved
- ✅ Custom values persist across reloads
- ✅ Model parser handles custom values gracefully

## Migration

### For Existing Experts
- No migration needed
- Existing model values continue to work
- Can add custom models anytime

### For New Settings
To add `allow_custom` to new settings:
```python
"new_setting": {
    "type": "str",
    "valid_values": ["opt1", "opt2"],
    "allow_custom": True,  # Add this line
}
```

## Limitations

### Current
1. No format validation for custom values
2. No autocomplete for custom values
3. Typos in custom values will cause errors at runtime

### Mitigation
- Clear tooltips explain required format (Provider/ModelName)
- Parser defaults to OpenAI for unknown formats
- Error logs show exact model string used

## Future Enhancements

### 1. Fetch Models from API
```python
# Could fetch models dynamically
"valid_values": fetch_naga_models_from_api(),
"allow_custom": True
```

### 2. Format Validation
```python
"allow_custom": True,
"custom_validator": validate_model_format,
"validation_message": "Format: Provider/ModelName"
```

### 3. Autocomplete
```python
"allow_custom": True,
"autocomplete_source": "https://api.naga.ac/v1/models"
```

### 4. Recently Used
Track recently used custom values:
```python
"allow_custom": True,
"show_recent": True  # Show recent custom values at top
```

## Summary

✅ **Implemented:**
- `allow_custom` flag in settings definitions
- Editable select UI component
- Applied to all TradingAgents model settings
- Backward compatible (default=False)

✅ **Benefits:**
- Future-proof for new models
- Flexible for power users
- No code updates needed for new models

✅ **Use Cases:**
- Naga AI paid tier models
- Beta/preview models
- Regional endpoints (future)
- Custom provider support

The feature is production-ready and fully functional! 🎉
