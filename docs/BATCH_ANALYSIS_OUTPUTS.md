# Batch Analysis Output Fetching Implementation

## Overview
Added `get_analysis_outputs_batch()` method to SmartRiskManagerToolkit to enable efficient fetching of multiple analysis outputs in a single call with automatic truncation handling.

## Problem Solved
Previously, the LLM had to make separate tool calls for each analysis output, which was:
- Inefficient (multiple round trips)
- Prone to token overflow without safeguards
- Difficult to manage when fetching many outputs

## Solution
New batch method that:
1. Accepts list of analysis IDs with their desired output keys
2. Fetches all requested outputs in a single operation
3. Automatically tracks total token count (chars / 4)
4. Truncates gracefully when approaching limits
5. Reports which items were included/skipped with reasons

## Changes Made

### File: `ba2_trade_platform/core/SmartRiskManagerToolkit.py`

#### New Method: `get_analysis_outputs_batch()`
**Location**: After `get_analysis_output_detail()` method

**Signature**:
```python
def get_analysis_outputs_batch(
    self,
    requests: List[Dict[str, Any]],  # [{"analysis_id": int, "output_keys": [str, ...]}]
    max_tokens: int = 100000
) -> Dict[str, Any]
```

**Request Format**:
```python
requests = [
    {"analysis_id": 123, "output_keys": ["analysis_summary", "market_report"]},
    {"analysis_id": 124, "output_keys": ["news_report", "sentiment_report"]}
]
```

**Response Format**:
```python
{
    "outputs": [
        {
            "analysis_id": 123,
            "output_key": "analysis_summary",
            "symbol": "AAPL",
            "content": "...",  # Full content
            "truncated": False,
            "original_length": 5000,
            "included_length": 5000
        },
        # ... more outputs
    ],
    "truncated": False,  # Whether any truncation occurred
    "skipped_items": [
        {
            "analysis_id": 125,
            "output_key": "fundamentals_report",
            "reason": "truncated_due_to_size_limit"
        }
    ],
    "total_chars": 45000,
    "total_tokens_estimate": 11250,  # chars / 4
    "items_included": 4,
    "items_skipped": 1
}
```

**Truncation Behavior**:
- Tracks running total of characters
- When approaching limit, includes partial content with `<TRUNCATED>` marker
- Only includes partial if >1000 chars remaining (otherwise skips entirely)
- Each output has `truncated` flag and `original_length` vs `included_length`

**Skip Reasons**:
- `analysis_not_found`: MarketAnalysis ID doesn't exist
- `expert_not_found`: Expert instance not found
- `method_not_implemented`: Expert doesn't implement get_output_detail()
- `truncated_due_to_size_limit`: Exceeded max_tokens
- `insufficient_space_remaining`: <1000 chars remaining
- `error: {message}`: Exception during fetch

**Updated get_tools()**: Added to tool list (now 13 tools total)

### File: `ba2_trade_platform/core/SmartRiskManagerGraph.py`

#### 1. New Tool Wrapper: `get_analysis_outputs_batch_tool()`
**Location**: After `get_analysis_output_detail_tool()` in both:
- Main tools (line ~418)
- Research node tools (line ~960)

**Docstring Highlights**:
```python
"""Fetch multiple analysis outputs efficiently in a single call.

Use this instead of calling get_analysis_output_detail_tool multiple times.
Automatically handles truncation if content exceeds max_tokens limit.
"""
```

#### 2. Updated RESEARCH_PROMPT
**Change**: Added recommendation to use batch tool
```
### Step 3: Read detailed outputs
**RECOMMENDED**: Use `get_analysis_outputs_batch_tool(requests)` to fetch multiple outputs efficiently in one call.
- Automatically handles token limits and truncation
- Example: `get_analysis_outputs_batch_tool([{"analysis_id": 123, "output_keys": ["analysis_summary", "market_report"]}, ...])`

**ALTERNATIVE**: Use `get_analysis_output_detail_tool(analysis_id, output_key)` to read outputs one at a time.
```

#### 3. Updated Tool Lists
Added `get_analysis_outputs_batch_tool` to:
- **Main tools list** (line ~550): 10 tools total
- **Research tools list** (line ~1030): 6 tools total

## Token Estimation

The method uses a simple approximation:
- **1 token ≈ 4 characters**
- Default limit: 100,000 tokens = 400,000 characters
- This is conservative (actual tokenization may vary)

## Use Cases

### 1. Fetch Multiple Outputs from One Analysis
```python
# Get summary + full reports from single analysis
requests = [{
    "analysis_id": 123,
    "output_keys": [
        "analysis_summary",
        "market_report", 
        "fundamentals_report",
        "sentiment_report"
    ]
}]
result = toolkit.get_analysis_outputs_batch(requests)
```

### 2. Fetch Same Output from Multiple Analyses
```python
# Compare analyst recommendations across multiple symbols
requests = [
    {"analysis_id": 123, "output_keys": ["final_trade_decision"]},
    {"analysis_id": 124, "output_keys": ["final_trade_decision"]},
    {"analysis_id": 125, "output_keys": ["final_trade_decision"]}
]
result = toolkit.get_analysis_outputs_batch(requests)
```

### 3. Mixed Batch with Size Limit
```python
# Fetch with 50K token limit
requests = [
    {"analysis_id": 123, "output_keys": ["analysis_summary", "market_report"]},
    {"analysis_id": 124, "output_keys": ["news_report"]},
    {"analysis_id": 125, "output_keys": ["investment_debate"]}
]
result = toolkit.get_analysis_outputs_batch(requests, max_tokens=50000)

# Check what was included
if result["truncated"]:
    print(f"Included {result['items_included']} outputs")
    print(f"Skipped {result['items_skipped']} outputs")
    for item in result["skipped_items"]:
        print(f"Skipped: {item['analysis_id']}/{item['output_key']} - {item['reason']}")
```

## Error Handling

The method is resilient to individual failures:
- If one analysis is not found, continues with others
- If one expert method fails, logs error and skips that item
- Always returns valid response structure
- Detailed skip reasons help debugging

## Testing

Created test script: `test_files/test_batch_outputs.py`

**Test Coverage**:
1. Single analysis, multiple outputs
2. Multiple analyses, multiple outputs each
3. Truncation handling with low token limit
4. Skip reason verification

**Run Test**:
```bash
.venv\Scripts\python.exe test_files\test_batch_outputs.py
```

## Performance Benefits

### Before (Individual Calls)
```
Agent → Call 1: get_analysis_output_detail(123, "summary")
Agent ← Response 1: "..." (5K chars)
Agent → Call 2: get_analysis_output_detail(123, "market_report") 
Agent ← Response 2: "..." (12K chars)
Agent → Call 3: get_analysis_output_detail(124, "news_report")
Agent ← Response 3: "..." (8K chars)

Total: 3 tool calls, 3 LLM iterations
```

### After (Batch Call)
```
Agent → Call: get_analysis_outputs_batch([
    {"analysis_id": 123, "output_keys": ["summary", "market_report"]},
    {"analysis_id": 124, "output_keys": ["news_report"]}
])
Agent ← Response: {
    "outputs": [...],  # All 3 outputs
    "total_chars": 25000,
    "truncated": false
}

Total: 1 tool call, 1 LLM iteration
```

**Improvement**: ~66% reduction in tool calls and LLM iterations

## Integration with SmartRiskManager

The SmartRiskManager agent can now:
1. Call `get_recent_analyses()` to get analysis IDs
2. Call `get_analysis_outputs()` to see available outputs
3. Call `get_analysis_outputs_batch()` to fetch multiple outputs efficiently
4. Check `truncated` flag and `skipped_items` to know if it needs to refine request

**Example Agent Flow**:
```
Agent: "I need to review AAPL, MSFT, GOOGL analyses"
→ get_recent_analyses(max_age_hours=24)
← Returns analysis IDs: 123 (AAPL), 124 (MSFT), 125 (GOOGL)

Agent: "Fetch key outputs from all three"
→ get_analysis_outputs_batch([
    {"analysis_id": 123, "output_keys": ["analysis_summary", "final_trade_decision"]},
    {"analysis_id": 124, "output_keys": ["analysis_summary", "final_trade_decision"]},
    {"analysis_id": 125, "output_keys": ["analysis_summary", "final_trade_decision"]}
])
← Returns all 6 outputs in one response

Agent: "Based on these analyses..." [makes decision]
```

## Backward Compatibility

- Existing `get_analysis_output_detail()` method unchanged
- Existing tools still work as before
- New batch method is purely additive
- Agents can use either method as appropriate

## Future Enhancements

Potential improvements:
1. Add output priority ranking (fetch high-priority first)
2. Add compression for very large outputs
3. Add caching to avoid re-fetching same outputs
4. Add parallel fetching for multiple analyses
5. Support regex patterns for output_keys (e.g., ".*_report")
