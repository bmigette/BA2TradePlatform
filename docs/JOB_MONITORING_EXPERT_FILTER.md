# Job Monitoring Expert Filter Feature

**Date:** October 9, 2025  
**Feature:** Expert filtering dropdown in Job Monitoring table  
**File:** `ba2_trade_platform/ui/pages/marketanalysis.py`

## Overview

Added an expert filter dropdown to the Job Monitoring tab, allowing users to filter market analysis jobs by specific expert instances. This complements the existing status and symbol filters.

## Implementation Details

### 1. State Management

**Added to `__init__` method:**
```python
self.expert_filter = 'all'  # Filter by expert
```

### 2. UI Component

**Added expert dropdown in `render()` method** (after status filter):
```python
# Expert filter
expert_options = self._get_expert_options()
self.expert_select = ui.select(
    options=expert_options,
    value='all',
    label='Expert Filter'
).classes('w-48')
self.expert_select.on_value_change(self._on_expert_filter_change)
```

### 3. Get Expert Options Method

**New method `_get_expert_options()`:**
```python
def _get_expert_options(self) -> dict:
    """Get available expert instances for filtering."""
    try:
        with get_db() as session:
            # Get all expert instances
            expert_instances = session.exec(select(ExpertInstance)).all()
            
            # Build options dictionary
            options = {'all': 'All Experts'}
            for expert in expert_instances:
                # Use user_description if available, otherwise expert name
                display_name = expert.user_description or expert.expert
                options[str(expert.id)] = display_name
            
            return options
    except Exception as e:
        logger.error(f"Error getting expert options: {e}", exc_info=True)
        return {'all': 'All Experts'}
```

**Features:**
- ✅ Dynamically loads all expert instances from database
- ✅ Uses `user_description` if available, otherwise falls back to expert name
- ✅ Returns ID as string key for dropdown value
- ✅ Always includes "All Experts" option
- ✅ Error handling with logging

### 4. Filter Change Handler

**New method `_on_expert_filter_change()`:**
```python
def _on_expert_filter_change(self, event):
    """Handle expert filter change."""
    self.expert_filter = event.value
    self.current_page = 1  # Reset to first page when filtering
    self.refresh_data()
```

**Behavior:**
- Updates `self.expert_filter` state
- Resets pagination to page 1 (consistent with other filters)
- Triggers data refresh

### 5. Database Filtering

**Updated `_get_analysis_data()` method:**

**Query filtering:**
```python
# Apply expert filter
if self.expert_filter != 'all':
    statement = statement.where(MarketAnalysis.expert_instance_id == int(self.expert_filter))
```

**Count filtering:**
```python
if self.expert_filter != 'all':
    count_statement = count_statement.where(MarketAnalysis.expert_instance_id == int(self.expert_filter))
```

**Features:**
- ✅ Filters both data query and count query
- ✅ Converts string ID to integer for database comparison
- ✅ Only applies filter when not 'all'

### 6. Clear Filters Integration

**Updated `_clear_filters()` method:**
```python
def _clear_filters(self):
    """Clear all filters."""
    self.status_filter = 'all'
    self.expert_filter = 'all'  # Added
    self.current_page = 1
    if hasattr(self, 'status_select'):
        self.status_select.value = 'all'
    if hasattr(self, 'expert_select'):
        self.expert_select.value = 'all'  # Added
    if hasattr(self, 'symbol_input'):
        self.symbol_input.value = ''
    self.refresh_data()
```

**Features:**
- ✅ Resets expert filter to 'all'
- ✅ Resets expert dropdown UI component
- ✅ Consistent with other filter reset behavior

## User Experience

### Filter Layout
```
[Status Filter ▼] [Expert Filter ▼] [Symbol Filter] | [Clear Filters] [Refresh] [Auto-refresh]
   (w-40)            (w-48)            (w-40)
```

### Dropdown Display Format
- **"All Experts"** - Shows all jobs (default)
- **"My Trading Agent"** - User description (if set)
- **"TradingAgents"** - Expert class name (fallback)

### Filter Interaction
1. User selects expert from dropdown
2. Table immediately refreshes with filtered data
3. Pagination resets to page 1
4. Record count updates to show filtered total
5. Can combine with status and symbol filters

### Clear Filters Behavior
- Resets all three filters: status, expert, and symbol
- Returns to "All Experts" option
- Resets to page 1
- Shows all records

## Technical Specifications

### Database Query Flow
```
_get_analysis_data()
    ↓
Build base query: select(MarketAnalysis)
    ↓
Apply status filter (if not 'all')
    ↓
Apply expert filter (if not 'all') ← NEW
    ↓
Build count query with same filters
    ↓
Apply pagination (offset, limit)
    ↓
Return data + total count
```

### Data Types
- **expert_filter state**: `str` (either 'all' or expert instance ID as string)
- **Database comparison**: `int` (converted from string for query)
- **Expert options**: `dict[str, str]` (ID → display name)

### Performance Considerations
- Expert dropdown loads once on render (not on every filter change)
- Expert list cached in dropdown options
- Query uses indexed `expert_instance_id` column for efficient filtering
- Count query runs in parallel with data query (single database session)

## Testing Recommendations

### 1. Basic Functionality
```python
# Test expert dropdown loads correctly
# Navigate to Job Monitoring tab
# Verify dropdown shows "All Experts" and all expert instances

# Test expert filtering
# Select specific expert from dropdown
# Verify only jobs from that expert are shown
# Verify record count updates correctly
```

### 2. Filter Combinations
```python
# Test expert + status filter
# Select expert: "TradingAgents"
# Select status: "completed"
# Verify only completed jobs from TradingAgents shown

# Test expert + symbol filter
# Select expert: "TradingAgents"
# Type symbol: "AAPL"
# Verify only AAPL jobs from TradingAgents shown

# Test all three filters
# Select expert, status, and symbol
# Verify AND logic works correctly
```

### 3. Pagination
```python
# Test pagination resets when filter changes
# Go to page 3
# Change expert filter
# Verify back on page 1
# Verify page count recalculated
```

### 4. Clear Filters
```python
# Set all three filters
# Click "Clear Filters"
# Verify expert dropdown shows "All Experts"
# Verify status shows "All Status"
# Verify symbol input is empty
# Verify all records shown
```

### 5. Edge Cases
```python
# Test no experts in database
# Verify dropdown shows only "All Experts"

# Test expert with no jobs
# Select expert with no jobs
# Verify empty table with correct message

# Test deleted expert instance
# If expert instance deleted but jobs remain
# Verify jobs still shown with "Unknown" expert name
```

## Files Modified

**File:** `ba2_trade_platform/ui/pages/marketanalysis.py`

**Changes:**
1. Line ~31: Added `self.expert_filter = 'all'` to `__init__`
2. Lines ~53-59: Added expert filter dropdown in `render()`
3. Lines ~351-368: Added `_get_expert_options()` method
4. Lines ~377-381: Added `_on_expert_filter_change()` method
5. Lines ~383-393: Updated `_clear_filters()` to include expert filter
6. Lines ~203-204: Added expert filter to query in `_get_analysis_data()`
7. Lines ~209-210: Added expert filter to count query in `_get_analysis_data()`

## Integration Points

### Database Models
- **ExpertInstance**: Source of expert list
  - `id`: Used as filter value
  - `user_description`: Preferred display name
  - `expert`: Fallback display name

- **MarketAnalysis**: Filtered table
  - `expert_instance_id`: Foreign key for filtering

### Existing Features
- ✅ Works with status filter (AND logic)
- ✅ Works with symbol filter (AND logic)
- ✅ Works with pagination
- ✅ Works with auto-refresh
- ✅ Works with Clear Filters button

## Future Enhancements

### Possible Improvements
1. **Multi-select**: Allow filtering by multiple experts simultaneously
2. **Expert grouping**: Group experts by type (e.g., "TradingAgents", "FinnhubRating")
3. **Expert statistics**: Show job count per expert in dropdown
4. **Favorite experts**: Pin frequently used experts to top
5. **Expert search**: Add search/autocomplete for large expert lists
6. **URL parameters**: Persist filter selection in URL for bookmarking

### Example Multi-Select Implementation
```python
# Replace ui.select with ui.element for multi-select
self.expert_select = ui.element('q-select').props(
    'multiple use-chips'
).bind_value(self, 'expert_filter')

# Update filtering logic
if self.expert_filter and self.expert_filter != ['all']:
    statement = statement.where(MarketAnalysis.expert_instance_id.in_(
        [int(id) for id in self.expert_filter]
    ))
```

## Conclusion

The expert filter dropdown provides users with a powerful way to focus on jobs from specific experts. It integrates seamlessly with existing filters, maintains consistent behavior with status and symbol filters, and provides a clean, intuitive user experience.

**Key Benefits:**
- ✅ Quick filtering by expert without manual searching
- ✅ Reduces visual clutter when many experts are active
- ✅ Enables expert-specific job analysis and monitoring
- ✅ Maintains performance with efficient database queries
- ✅ Consistent UX with other filter controls
