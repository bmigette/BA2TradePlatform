# Balance Usage Chart JavaScript Formatter Fix

**Date:** October 13, 2025  
**Status:** ✅ Fixed (Multiple Issues)

## Problems Fixed

### Issue 1: JavaScript Code in Bar Labels
Bar labels showing JavaScript code instead of formatted dollar values.

### Issue 2: JavaScript Code in Tooltip
Tooltips showing raw JavaScript function code instead of formatted values.

## Root Causes

1. **Array Concatenation**: Formatter used `str(total_per_expert)` which created string literals
2. **Multi-line Strings**: Triple-quoted strings (`'''`) caused interpretation issues in NiceGUI/ECharts

## Solutions Applied

### Fix 1: MarkPoint Formatter (Bar Labels)
**Before:**
```python
'formatter': '''function(params) {
    var index = params.dataIndex;
    var total = ''' + str(total_per_expert).replace("'", '"') + '''[index];
    return '$' + total.toFixed(2);
}'''
```

**After:**
```python
# Balance Usage Chart Formatter Fix

## Date
2025-01-XX

## Problems Identified

### Problem 1: JavaScript Code Showing in Bar Labels and Tooltips
**Description:** The Balance Usage Per Expert chart was showing literal JavaScript code instead of formatted values:
- Bar labels showed: `function(params) { return "$" + params.value.toFixed(2); }`
- Tooltips showed similar JavaScript function code

**Visual Issue:** Users saw JavaScript function strings displayed as text on the chart.

### Problem 2: NiceGUI ECharts Doesn't Support JavaScript Functions
**Root Discovery:** NiceGUI's ECharts wrapper does NOT execute JavaScript function strings passed in formatters. It only supports ECharts template strings.

## Root Cause Analysis

### Why JavaScript Functions Don't Work
NiceGUI's ECharts implementation passes formatter strings directly to the ECharts library without JavaScript evaluation. The correct approach is to use **ECharts template strings**, not JavaScript functions.

### Evidence from Codebase
Other working charts in the codebase use template strings:
```python
# From ProfitPerExpertChart.py (line 145)
'formatter': '${c}'  # Works ✅

# From InstrumentDistributionChart.py (line 163)
'formatter': '{b}\n{d}%'  # Works ✅

# Attempted approach (doesn't work)
'formatter': 'function(params) { return "$" + params.value; }'  # Fails ❌
```

### ECharts Template String Syntax
- `{a}` - Series name
- `{b}` - Data name (x-axis label)
- `{c}` - Data value
- `{d}` - Percentage (for pie charts)
- Can use `<br/>` for line breaks
- Can add literal text: `'${c}'` displays as "$100.00"

## Solutions Implemented

### Solution 1: MarkPoint Label - Template String
Changed from JavaScript function to ECharts template string:

**Before:**
```python
'formatter': 'function(params) { return "$" + params.value.toFixed(2); }'
```

**After:**
```python
'formatter': '${c}'  # ECharts template string: {c} = value
```

**Location:** `ba2_trade_platform/ui/components/BalanceUsagePerExpertChart.py` line ~287

### Solution 2: Tooltip - Removed Complex Formatter
Removed JavaScript formatter entirely because:
1. Template strings cannot calculate totals (sum of two series)
2. Template strings have limited conditional logic
3. Default ECharts tooltip provides adequate formatting

**Before:**
```python
'formatter': '''function(params) {
    var total = params[0].value + params[1].value;
    return params[0].name + '<br/>' +
           'Filled Orders: $' + params[0].value.toFixed(2) + '<br/>' +
           'Pending Orders: $' + params[1].value.toFixed(2) + '<br/>' +
           'Total: $' + total.toFixed(2);
}'''
```

**After:**
```python
'tooltip': {
    'trigger': 'axis',
    'axisPointer': {'type': 'shadow'}
    # Complex tooltip with total calculation would require JavaScript.
    # Using default tooltip which shows series values separately.
}
```

**Location:** `ba2_trade_platform/ui/components/BalanceUsagePerExpertChart.py` line ~213-221

**Trade-off:** Lost ability to show "Total: $X" in tooltip, but gained working display without JavaScript code.

## Files Modified

- `ba2_trade_platform/ui/components/BalanceUsagePerExpertChart.py`
  - Line ~287: Changed markPoint formatter to template string `'${c}'`
  - Line ~213-221: Removed tooltip formatter, using default behavior

## Testing

### Manual Testing Steps
1. Navigate to the Balance Usage Per Expert chart page
2. Verify bar labels show formatted dollar amounts (e.g., "$50.00") at the top of each bar
3. Hover over bars to verify tooltip shows:
   - Expert name
   - Filled Orders: $X.XX (as separate line)
   - Pending Orders: $Y.YY (as separate line)
   - **Note:** Total no longer shown (limitation of template strings)
4. Check that NO JavaScript code is visible anywhere

### Expected Results
- ✅ Bar labels show `$X.XX` format without JavaScript code
- ✅ Tooltip shows series values separately with proper formatting
- ✅ No literal JavaScript code visible anywhere in the chart
- ⚠️ Tooltip no longer shows total (requires JavaScript calculation)

## Impact

### User Experience
- Chart now displays correctly without JavaScript code
- Bar labels properly formatted
- Tooltip functional but simplified (no total calculation)
- Professional appearance maintained

### Code Quality
- Follows NiceGUI ECharts best practices
- Uses proper template string syntax
- More maintainable than attempted JavaScript functions
- Consistent with other charts in codebase

### Limitations Accepted
- Cannot show calculated total in tooltip (would require JavaScript)
- Template strings have limited formatting capabilities
- Trade-off for working, maintainable code

## Key Learnings

### NiceGUI ECharts Formatter Rules
1. ✅ **DO**: Use ECharts template strings like `'{b}<br/>${c}'`
2. ❌ **DON'T**: Try to pass JavaScript function strings
3. ✅ **DO**: Pre-calculate complex values in Python
4. ❌ **DON'T**: Attempt calculations in formatters (use default tooltip)

### Alternative Approaches for Complex Formatting
If complex formatting/calculations are needed:
1. Calculate values in Python before passing to chart
2. Use separate UI elements to display totals
3. Use text annotations instead of formatters
4. Accept default ECharts formatting

## Related Issues
- Price cache improvements (global caching)
- P/L widget async loading
- Chart performance optimizations
```

### Fix 2: Tooltip Formatter
**Before:**
```python
'formatter': '''function(params) {
    var total = params[0].value + params[1].value;
    return params[0].name + '<br/>' + ...;
}'''
```

**After:**
```python
'formatter': 'function(params) { var total = params[0].value + params[1].value; return params[0].name + "<br/>" + params[0].marker + " " + params[0].seriesName + ": $" + params[0].value.toFixed(2) + "<br/>" + params[1].marker + " " + params[1].seriesName + ": $" + params[1].value.toFixed(2) + "<br/>" + "<b>Total: $" + total.toFixed(2) + "</b>"; }'
```

## Key Changes

1. ✅ Single-line strings for JavaScript functions
2. ✅ Use `params.value` directly instead of array lookups
3. ✅ Double quotes (`"`) for HTML inside JavaScript
4. ✅ Single quotes (`'`) for Python string wrapper

## Files Modified

- `ba2_trade_platform/ui/components/BalanceUsagePerExpertChart.py`
  - Fixed markPoint label formatter (line ~287)
  - Fixed tooltip formatter (line ~221)

## Testing

Navigate to Overview page and verify:
- ✅ Bar labels show "$1234.56" format
- ✅ Tooltips show formatted values on hover
- ✅ No JavaScript code visible anywhere
- ✅ All values formatted with 2 decimal places

## Impact

- ✅ User Experience: Proper formatting throughout chart
- ✅ Code Quality: Simplified, maintainable code
- ✅ Backward Compatible: No breaking changes
