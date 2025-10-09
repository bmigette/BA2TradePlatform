# Risk Manager Prompt Centralization

**Date:** October 9, 2025  
**Issue:** Risk manager prompt was embedded in code instead of centralized in prompts.py

## Problem

The risk manager prompt was hardcoded as an f-string directly in the `risk_manager.py` file, breaking the pattern used by all other agents in the TradingAgents framework.

### Issues with Embedded Prompts

1. **Inconsistency:** All other prompts (analysts, researchers, managers, traders) were centralized in `prompts.py`
2. **Maintainability:** Updating the risk manager prompt required editing code logic file instead of the prompt library
3. **Discoverability:** The prompt was hidden in the implementation, making it hard to find and review
4. **Versioning:** No single source of truth for prompt changes and improvements
5. **Testing:** Difficult to test prompt variations without modifying core logic

### Location Before Fix

**File:** `tradingagents/agents/managers/risk_manager.py` (line ~21-40)
```python
prompt = f"""As the Risk Management Judge and Debate Facilitator, your goal is to evaluate the debate between three risk analysts—Risky, Neutral, and Safe/Conservative—and determine the best course of action for the trader. Your decision must result in a clear recommendation: Buy, Sell, or Hold. Choose Hold only if strongly justified by specific arguments, not as a fallback when all sides seem valid. Strive for clarity and decisiveness.

Guidelines for Decision-Making:
1. **Summarize Key Arguments**: Extract the strongest points from each analyst, focusing on relevance to the context.
2. **Provide Rationale**: Support your recommendation with direct quotes and counterarguments from the debate.
3. **Refine the Trader's Plan**: Start with the trader's original plan, **{trader_plan}**, and adjust it based on the analysts' insights.
4. **Learn from Past Mistakes**: Use lessons from **{past_memory_str}** to address prior misjudgments and improve the decision you are making now to make sure you don't make a wrong BUY/SELL/HOLD call that loses money.

Deliverables:
- A clear and actionable recommendation: Buy, Sell, or Hold.
- Detailed reasoning anchored in the debate and past reflections.

---

**Analysts Debate History:**  
{history}

---

Focus on actionable insights and continuous improvement. Build on past lessons, critically evaluate all perspectives, and ensure each decision advances better outcomes."""
```

## Solution

Moved the risk manager prompt to `prompts.py` following the established pattern.

### Changes Made

#### 1. Added Prompt to prompts.py

**Location:** Under "MANAGER PROMPTS" section (after RESEARCH_MANAGER_PROMPT)

```python
RISK_MANAGER_PROMPT = """As the Risk Management Judge and Debate Facilitator, your goal is to evaluate the debate between three risk analysts—Risky, Neutral, and Safe/Conservative—and determine the best course of action for the trader. Your decision must result in a clear recommendation: Buy, Sell, or Hold. Choose Hold only if strongly justified by specific arguments, not as a fallback when all sides seem valid. Strive for clarity and decisiveness.

Guidelines for Decision-Making:
1. **Summarize Key Arguments**: Extract the strongest points from each analyst, focusing on relevance to the context.
2. **Provide Rationale**: Support your recommendation with direct quotes and counterarguments from the debate.
3. **Refine the Trader's Plan**: Start with the trader's original plan, **{trader_plan}**, and adjust it based on the analysts' insights.
4. **Learn from Past Mistakes**: Use lessons from **{past_memory_str}** to address prior misjudgments and improve the decision you are making now to make sure you don't make a wrong BUY/SELL/HOLD call that loses money.

Deliverables:
- A clear and actionable recommendation: Buy, Sell, or Hold.
- Detailed reasoning anchored in the debate and past reflections.

---

**Analysts Debate History:**  
{history}

---

Focus on actionable insights and continuous improvement. Build on past lessons, critically evaluate all perspectives, and ensure each decision advances better outcomes."""
```

#### 2. Added Helper Function

```python
def format_risk_manager_prompt(**kwargs) -> str:
    """Format risk manager prompt with provided variables"""
    return RISK_MANAGER_PROMPT.format(**kwargs)
```

#### 3. Added to PROMPT_REGISTRY

```python
PROMPT_REGISTRY = {
    # ... other prompts ...
    
    # Managers
    "research_manager": RESEARCH_MANAGER_PROMPT,
    "risk_manager": RISK_MANAGER_PROMPT,  # ← Added
    
    # ... other prompts ...
}
```

#### 4. Updated risk_manager.py

**Before:**
```python
import time
import json

def create_risk_manager(llm, memory):
    def risk_manager_node(state) -> dict:
        # ... gather state data ...
        
        prompt = f"""As the Risk Management Judge..."""  # ❌ Hardcoded
        response = llm.invoke(prompt)
```

**After:**
```python
import time
import json
from ...prompts import format_risk_manager_prompt  # ✅ Import helper

def create_risk_manager(llm, memory):
    def risk_manager_node(state) -> dict:
        # ... gather state data ...
        
        prompt = format_risk_manager_prompt(  # ✅ Use helper function
            trader_plan=trader_plan,
            past_memory_str=past_memory_str,
            history=history
        )
        response = llm.invoke(prompt)
```

## Benefits

1. ✅ **Consistency:** Risk manager now follows the same pattern as all other agents
2. ✅ **Centralization:** All prompts in one place (`prompts.py`)
3. ✅ **Maintainability:** Prompt changes don't require touching logic code
4. ✅ **Discoverability:** Easy to find and review all prompts
5. ✅ **Testability:** Can test prompt variations separately from logic
6. ✅ **Registry Access:** Available via `get_prompt("risk_manager")`

## Usage Examples

### Direct Import
```python
from tradingagents.prompts import RISK_MANAGER_PROMPT

# Use the raw template
prompt = RISK_MANAGER_PROMPT.format(
    trader_plan="...",
    past_memory_str="...",
    history="..."
)
```

### Helper Function
```python
from tradingagents.prompts import format_risk_manager_prompt

# Use the helper for cleaner syntax
prompt = format_risk_manager_prompt(
    trader_plan=trader_plan,
    past_memory_str=past_memory_str,
    history=history
)
```

### Registry Access
```python
from tradingagents.prompts import get_prompt

# Get from registry
template = get_prompt("risk_manager")
prompt = template.format(
    trader_plan="...",
    past_memory_str="...",
    history="..."
)
```

## Prompt Variables

The risk manager prompt accepts these variables:

| Variable | Type | Description |
|----------|------|-------------|
| `trader_plan` | str | The trader's original investment plan |
| `past_memory_str` | str | Reflections from similar past situations |
| `history` | str | Full debate history from risk analysts |

## Pattern for All Managers

All manager prompts now follow this consistent structure:

1. **Define in prompts.py:** `MANAGER_NAME_PROMPT = """..."""`
2. **Add helper function:** `def format_manager_name_prompt(**kwargs) -> str:`
3. **Register:** Add to `PROMPT_REGISTRY`
4. **Use in manager:** Import and call `format_manager_name_prompt()`

### Current Manager Prompts

- ✅ `RESEARCH_MANAGER_PROMPT` - Portfolio manager and debate facilitator
- ✅ `RISK_MANAGER_PROMPT` - Risk management judge (newly centralized)

## Files Modified

1. **`tradingagents/prompts.py`**
   - Added `RISK_MANAGER_PROMPT` constant
   - Added `format_risk_manager_prompt()` helper
   - Added registry entry

2. **`tradingagents/agents/managers/risk_manager.py`**
   - Added import: `from ...prompts import format_risk_manager_prompt`
   - Replaced hardcoded f-string with helper function call
   - Reduced file from ~60 lines to ~30 lines (cleaner)

## Testing

No functional changes - the prompt content and variable substitution remain identical. This is a pure refactoring for code organization.

To verify:
```python
from tradingagents.prompts import format_risk_manager_prompt

prompt = format_risk_manager_prompt(
    trader_plan="Test plan",
    past_memory_str="Past lessons",
    history="Debate history"
)

# Should contain all three variables
assert "Test plan" in prompt
assert "Past lessons" in prompt
assert "Debate history" in prompt
```

## Related Documentation

- **Prompts Library:** `tradingagents/prompts.py` - All TradingAgents prompts
- **Risk Manager:** `tradingagents/agents/managers/risk_manager.py` - Implementation
- **Prompt Registry:** See `PROMPT_REGISTRY` dict in prompts.py for all available prompts

## Future Improvements

Consider:
1. Add type hints to prompt variables for better IDE support
2. Create prompt validation functions to ensure required variables are provided
3. Add prompt versioning to track changes over time
4. Consider creating a base `ManagerPrompt` class for type safety
