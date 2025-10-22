# Date/Timezone Handling Standards

## Overview

All DateTime fields in the BA2 Trade Platform follow a strict standard:
- **Storage**: All dates stored in UTC (timezone.utc)
- **Display**: All dates shown to users in local timezone
- **Input**: All user input dates converted to UTC before storage

## DateTime Fields in Models

### ExpertRecommendation
- **created_at**: DateTime
  - Storage: `DateTime.now(timezone.utc)`
  - Usage: Timestamp when recommendation was created
  - Display: Show in local timezone with date + time

### MarketAnalysis
- **created_at**: DateTime
  - Storage: `DateTime.now(timezone.utc)`
  - Usage: Timestamp when analysis was created
  - Display: Show in local timezone with date + time

### AnalysisOutput
- **created_at**: DateTime
  - Storage: `DateTime.now(timezone.utc)`
  - Usage: Timestamp when output was generated
  - Display: Show in local timezone with date + time
- **start_date**: Optional[DateTime]
  - Storage: UTC timezone
  - Usage: Analysis data start date (e.g., for news/indicators)
  - Display: Show in local timezone
- **end_date**: Optional[DateTime]
  - Storage: UTC timezone
  - Usage: Analysis data end date
  - Display: Show in local timezone

### Transaction
- **open_date**: Optional[DateTime]
  - Storage: UTC timezone
  - Usage: When transaction was opened/entered
  - Display: Show in local timezone with date + time
- **close_date**: Optional[DateTime]
  - Storage: UTC timezone
  - Usage: When transaction was closed/exited
  - Display: Show in local timezone with date + time
- **created_at**: DateTime
  - Storage: `DateTime.now(timezone.utc)`
  - Usage: When transaction record was created
  - Display: Show in local timezone with date + time

### TradingOrder
- **created_at**: Optional[DateTime]
  - Storage: `DateTime.now(timezone.utc)`
  - Usage: When order was created
  - Display: Show in local timezone with date + time

### TradeActionResult
- **created_at**: DateTime
  - Storage: `DateTime.now(timezone.utc)`
  - Usage: When action was executed
  - Display: Show in local timezone with date + time

## Current Status

### ✅ Models (CORRECT)
All model definitions use `DateTime.now(timezone.utc)` as default factory:

```python
created_at: DateTime | None = Field(default_factory=lambda: DateTime.now(timezone.utc))
open_date: DateTime | None = Field(default=None)  # Stored as UTC when set
```

### ⚠️ UI Display (NEEDS AUDIT)
Need to verify all UI pages convert UTC to local timezone before display:
- marketanalysis.py
- marketanalysishistory.py
- market_analysis_detail.py
- overview.py
- performance.py
- settings.py

### ⚠️ API/Data Providers (NEEDS AUDIT)
Need to verify all external data is converted to UTC before storage:
- FMP (Senate Trading API)
- YFinance
- Alpaca
- Custom data providers

## Utility Module: date_utils.py

Use these functions for all date conversions:

### Functions Available

#### `utc_to_local(utc_dt: Optional[datetime]) -> Optional[datetime]`
Convert UTC datetime to local timezone

```python
from ba2_trade_platform.core.date_utils import utc_to_local

utc_dt = market_analysis.created_at  # UTC from database
local_dt = utc_to_local(utc_dt)
ui.label(f"Created: {local_dt.strftime('%Y-%m-%d %H:%M:%S')}")
```

#### `format_for_display(dt: Optional[datetime], format_string: str) -> str`
Format datetime for UI display (auto-converts to local timezone)

```python
from ba2_trade_platform.core.date_utils import format_for_display

created_str = format_for_display(market_analysis.created_at)
ui.label(f"Created: {created_str}")  # Shows "2025-10-22 09:15:30 PDT"

# Custom format
created_short = format_for_display(market_analysis.created_at, "%m/%d/%Y %I:%M %p")
ui.label(f"Created: {created_short}")  # Shows "10/22/2025 09:15 AM"
```

#### `format_relative(dt: Optional[datetime]) -> str`
Format datetime as relative time

```python
from ba2_trade_platform.core.date_utils import format_relative

ui.label(f"Created: {format_relative(market_analysis.created_at)}")
# Shows "2 hours ago", "yesterday", "in 3 days", etc.
```

#### `local_to_utc(local_dt: Optional[datetime]) -> Optional[datetime]`
Convert local timezone datetime to UTC (for user input)

```python
from ba2_trade_platform.core.date_utils import local_to_utc

user_input = datetime(2025, 10, 22, 14, 30, 0)  # User enters 2:30 PM
utc_dt = local_to_utc(user_input)
transaction.open_date = utc_dt
```

#### `get_utc_now() -> datetime`
Get current time in UTC

```python
from ba2_trade_platform.core.date_utils import get_utc_now

# Use instead of datetime.now(timezone.utc)
now_utc = get_utc_now()
```

#### `ensure_utc(dt: Optional[datetime]) -> Optional[datetime]`
Ensure a datetime is in UTC (defensive conversion)

```python
from ba2_trade_platform.core.date_utils import ensure_utc

# Safe to call even if already UTC
utc_dt = ensure_utc(some_datetime)
db_model.created_at = utc_dt
```

## UI Component Patterns

### Pattern 1: Display Created/Modified Timestamp
```python
from ba2_trade_platform.core.date_utils import format_for_display

with ui.row():
    ui.label("Created:").classes('font-bold')
    ui.label(format_for_display(market_analysis.created_at))
```

### Pattern 2: Display with Relative Time
```python
from ba2_trade_platform.core.date_utils import format_relative, format_for_display

with ui.row():
    ui.label("Created:").classes('font-bold')
    ui.label(format_relative(market_analysis.created_at))
    ui.label(f"({format_for_display(market_analysis.created_at)})").classes('text-gray-500 text-sm')
```

### Pattern 3: Display Date Range
```python
from ba2_trade_platform.core.date_utils import format_for_display

if output.start_date and output.end_date:
    ui.label(f"Period: {format_for_display(output.start_date, '%Y-%m-%d')} to {format_for_display(output.end_date, '%Y-%m-%d')}")
```

### Pattern 4: Display Transaction Dates
```python
from ba2_trade_platform.core.date_utils import format_for_display

with ui.column():
    if transaction.open_date:
        ui.label(f"Opened: {format_for_display(transaction.open_date)}")
    if transaction.close_date:
        ui.label(f"Closed: {format_for_display(transaction.close_date)}")
```

## Data Provider Integration

### When Fetching External Data
```python
from datetime import datetime, timezone
from ba2_trade_platform.core.date_utils import ensure_utc

# From FMP API
senate_trade = fmp_api.get_senate_trades()  # Returns ISO string or datetime
trade_date = datetime.fromisoformat(senate_trade['transactionDate'])  # Naive datetime
trade_date_utc = ensure_utc(trade_date)  # Ensure UTC
transaction.open_date = trade_date_utc
```

### When Converting API Strings to UTC
```python
from datetime import datetime, timezone

# From API response (usually ISO 8601)
api_date_str = "2025-10-22T14:30:00"  # ISO format, often assumed UTC
api_date = datetime.fromisoformat(api_date_str)  # Naive datetime
api_date_utc = api_date.replace(tzinfo=timezone.utc)  # Assume UTC
transaction.created_at = api_date_utc
```

## Audit Checklist

### Models Audit
- ✅ ExpertRecommendation.created_at: Uses `DateTime.now(timezone.utc)`
- ✅ MarketAnalysis.created_at: Uses `DateTime.now(timezone.utc)`
- ✅ AnalysisOutput.created_at: Uses `DateTime.now(timezone.utc)`
- ✅ AnalysisOutput.start_date: Optional but should be UTC
- ✅ AnalysisOutput.end_date: Optional but should be UTC
- ✅ Transaction.open_date: Optional but should be UTC
- ✅ Transaction.close_date: Optional but should be UTC
- ✅ Transaction.created_at: Uses `DateTime.now(timezone.utc)`
- ✅ TradingOrder.created_at: Uses `DateTime.now(timezone.utc)`
- ✅ TradeActionResult.created_at: Uses `DateTime.now(timezone.utc)`

### UI Pages Audit (TODO)
- [ ] marketanalysis.py: Audit date displays
- [ ] marketanalysishistory.py: Audit date displays
- [ ] market_analysis_detail.py: Audit date displays
- [ ] overview.py: Audit date displays
- [ ] performance.py: Audit date displays
- [ ] settings.py: Audit date displays

### Data Providers Audit (TODO)
- [ ] FMPSenateTraderCopy: Ensure dates converted to UTC
- [ ] YFinance: Ensure dates converted to UTC
- [ ] Alpaca: Ensure dates converted to UTC
- [ ] Custom providers: Ensure dates converted to UTC

## Testing

### Test: Database Storage
```python
from datetime import datetime, timezone
from ba2_trade_platform.core.models import MarketAnalysis
from ba2_trade_platform.core.db import add_instance, get_instance

# Create analysis
analysis = MarketAnalysis(symbol="AAPL", expert_instance_id=1)
analysis_id = add_instance(analysis)

# Retrieve and verify UTC storage
analysis_from_db = get_instance(MarketAnalysis, analysis_id)
assert analysis_from_db.created_at.tzinfo == timezone.utc, "Date should be in UTC"
print(f"Stored: {analysis_from_db.created_at}")  # Should show UTC time
```

### Test: UI Display Conversion
```python
from datetime import datetime, timezone
from ba2_trade_platform.core.date_utils import format_for_display, utc_to_local

# Create UTC time
utc_now = datetime.now(timezone.utc)
print(f"UTC: {utc_now}")  # 2025-10-22 16:30:00+00:00

# Display in local
local_str = format_for_display(utc_now)
print(f"Local: {local_str}")  # 2025-10-22 09:30:00 PDT (example for PST)
```

## Migration Notes

If database contains naive datetimes (no timezone):
1. Assume all existing datetimes are in UTC
2. Add timezone info in migration or data loading code
3. Use `ensure_utc()` when reading from database

## Future Enhancements

1. **User Timezone Preference**: Read from user settings instead of system timezone
2. **Timezone Display**: Show timezone abbreviation in UI (PDT, EST, etc.)
3. **Scheduled Reports**: Generate timezone-aware timestamps
4. **Data Export**: Include timezone information in exports (CSV, etc.)
