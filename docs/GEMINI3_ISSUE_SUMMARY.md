# Gemini 3 Pro Preview - ToolMessage Name Field Issue

## Summary
Test confirms that Gemini 3 Pro Preview **requires** the `name` field in ToolMessage objects for function calling, but LangChain's ToolNode does not populate this field by default.

## Test Results

### Test 1: OpenAI GPT-4o (Control)
- **Status**: Works (tools execute successfully)
- **ToolMessage.name**: MISSING (empty)
- **Conclusion**: OpenAI models don't require the name field

### Test 2: Gemini 3 Pro Preview (Reproducing Issue)
- **Status**: FAILED with error
- **ToolMessage.name**: MISSING (empty)
- **Error**: `GenerateContentRequest.contents[2].parts[0].function_response.name: Name cannot be empty`
- **Conclusion**: **ISSUE CONFIRMED** - Gemini requires name field

### Test 3: Gemini with Manual Name Fix (Workaround Attempt)
- **Status**: FAILED (same error)
- **ToolMessage.name**: SET to 'get_stock_price' (has name attr: True, value: 'get_stock_price')
- **Error**: Same "Name cannot be empty" error
- **Conclusion**: Setting the `name` parameter in ToolMessage constructor doesn't help - LangChain/OpenAI client may not serialize it properly

## Root Cause Analysis

1. **LangChain ToolNode** creates ToolMessage objects without the `name` field
2. **Gemini API** requires the `name` field in function_response messages
3. **ToolMessage class** accepts a `name` parameter, but...
4. **LangChain's OpenAI client** doesn't serialize the `name` field when converting ToolMessages to API format

## The Problem Chain

```
ToolNode (no name) 
  → ToolMessage(content=..., tool_call_id=...) 
  → LangChain OpenAI Client 
  → API Request to Gemini 
  → ERROR: "Name cannot be empty"
```

Even when we try:
```python
ToolMessage(content=..., tool_call_id=..., name='tool_name')
```

The name still doesn't get transmitted to Gemini's API.

## Why Our Fixes Didn't Work

The fixes we implemented in the platform:
- ✅ Added name to ToolMessage objects in LoggingToolNode
- ✅ Added name to analyst node ToolMessages
- ✅ Created utility functions to fix messages

But they still failed because **LangChain's conversion layer doesn't send the name field to the API** even when it's set on the ToolMessage object.

## Possible Solutions

### Option 1: LangChain Update
Wait for LangChain to fix the OpenAI client to properly serialize ToolMessage.name field

### Option 2: Direct Gemini SDK
Use Gemini's native Python SDK instead of OpenAI compatibility layer

### Option 3: Custom Message Converter
Override LangChain's message conversion to ensure name field is included

### Option 4: Use Different Model
Stick with models that don't require the name field (OpenAI, Claude, etc.)

## Recommendation

**For now, keep Gemini 3 Pro Preview commented out** until one of the following occurs:
- LangChain releases a fix for ToolMessage.name serialization
- Gemini makes the name field optional
- We implement a custom message converter

The issue is confirmed and reproducible. It's not a configuration problem but a LangChain/API compatibility issue.
