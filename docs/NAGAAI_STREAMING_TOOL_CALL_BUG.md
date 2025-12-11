# NagaAI/Grok Streaming + Tool Calls Bug

## Issue Summary

NagaAI (api.naga.ac) has a critical bug when **streaming mode is enabled** and the model attempts to make **tool calls**. This affects Grok models and potentially other models served through NagaAI.

## Bug Behavior

When `streaming=True` is set on `ChatOpenAI` with NagaAI backend:

### 1. Tool Arguments Get Lost
```python
# With streaming=True (BROKEN)
Tool calls: 1
  name: 'get_indicator_data'
  id: '60644a297dd74b668c2fff134cf7ae05' (32 chars, UUID-like)
  args: {}  # <-- Arguments are EMPTY!

# With streaming=False (CORRECT)
Tool calls: 1
  name: 'get_indicator_data'
  id: 'call_10531672' (13 chars)
  args: {'indicator': 'rsi', 'symbol': 'NVDA'}  # <-- Arguments present
```

### 2. Tool Names Get Concatenated
In more severe cases, multiple tool names are concatenated into a single string:
```
get_ohlcv_dataget_indicator_dataget_indicator_dataget_indicator_data...
```

This happens when the model tries to call multiple tools - instead of returning them as separate tool calls, NagaAI concatenates all the tool names into one corrupted string.

### 3. Call IDs Also Corrupted
The tool call IDs also become extremely long (288+ characters instead of normal 13 characters) and appear to be concatenations of multiple IDs.

## Root Cause

The issue is in NagaAI's streaming response handler. When streaming is enabled:
1. Tool call chunks are not properly accumulated
2. Arguments are lost during the streaming aggregation
3. Multiple tool calls get merged together incorrectly

This is **NOT** an issue with:
- OpenAI's native API (streaming works fine)
- LangChain's tool handling
- The `parallel_tool_calls` parameter (bug occurs even with `parallel_tool_calls=False`)

## Detection

The platform includes detection for concatenated tool names in `db_storage.py`:

```python
def _check_concatenated_tool_name(self, tool_name: str) -> None:
    """Check for concatenated tool names caused by buggy LLM providers."""
    # Checks if any valid tool name appears multiple times in the string
    # Raises ToolCallFailureError if detected
```

## Fix Applied

In `TradingAgents.py`, a new expert setting `enable_streaming` has been added:

```python
"enable_streaming": {
    "type": "bool", "required": False, "default": True,
    "description": "Enable LLM Streaming",
    "tooltip": "When enabled, LLM responses stream incrementally which prevents timeouts on long operations. DISABLE for NagaAI/Grok models as streaming causes tool call arguments to be lost and tool names to get concatenated."
}
```

The setting is passed to `trading_graph.py` which uses it when creating the LLM:

```python
streaming_enabled = self.config.get("enable_streaming", None)
if streaming_enabled is None:
    # Fall back to global config
    from ba2_trade_platform import config as ba2_config
    streaming_enabled = ba2_config.OPENAI_ENABLE_STREAMING
```

## Why Streaming is Enabled by Default

For **non-NagaAI providers** (OpenAI, etc.), streaming has critical benefits:
1. **Avoids Cloudflare timeouts** - Long-running LLM responses can exceed Cloudflare's timeout limits. Streaming keeps the connection alive with incremental data.
2. **Faster initial response times** - incremental delivery reduces perceived latency
3. **Better UX** - users see content appearing progressively

These benefits work correctly with OpenAI's native API.

## Tradeoff for NagaAI

| Setting | Tool Calls | Long Operations |
|---------|------------|-----------------|
| `streaming=True` | ❌ BROKEN | ✅ No timeout |
| `streaming=False` | ✅ Works | ⚠️ Risk of Cloudflare timeout |

**Decision**: Disable streaming for NagaAI because:
- Broken tool calls = immediate complete failure (analysis cannot proceed)
- Cloudflare timeout = recoverable (can retry the operation)

If you experience timeouts with NagaAI, the workaround is to use shorter prompts or switch to a different provider.

## Test Results

### Test: `test_files/test_nagaai_langgraph.py`

| Setting | Tool Args | Tool IDs | Result |
|---------|-----------|----------|--------|
| `streaming=True` | Empty `{}` | 32 chars (UUID) | ❌ BROKEN |
| `streaming=False` | Correct | 13 chars | ✅ WORKS |

### Test: `test_files/test_nagaai_realistic.py`

Without streaming, tool calling works correctly even with complex prompts that request multiple tools.

## Workarounds

1. **Per-Expert Setting (Recommended)**: Disable streaming in the TradingAgents expert settings UI for NagaAI/Grok-based experts
2. **Global**: Set `OPENAI_ENABLE_STREAMING=false` in `.env` file (affects all providers)
3. **Use Different Provider**: OpenAI's native API handles streaming correctly

## Affected Models

All models served through NagaAI (api.naga.ac) are affected:
- `NagaAI/grok-*` (Grok 4 family)
- `NagaAC/grok-*` (Grok 4.1 family)
- `NagaAI/gpt-5-*` (GPT-5 family)
- `NagaAC/gpt-5.1-*`
- `NagaAI/qwen*`
- `NagaAI/deepseek*`
- `NagaAI/kimi*`

## Recommendation

Report this bug to NagaAI support with the reproduction steps in `test_files/test_nagaai_langgraph.py`.

## Date Discovered

December 10, 2025

## Related Files

- `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/trading_graph.py` - Fix applied
- `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/db_storage.py` - Detection logic
- `test_files/test_nagaai_langgraph.py` - Reproduction test
- `ba2_trade_platform/config.py` - Streaming configuration
