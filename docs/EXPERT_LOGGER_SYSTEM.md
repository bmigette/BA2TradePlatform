# Expert Logger System Implementation

## Summary

Implemented a streamlined, unified logging system for all expert instances across the BA2 Trade Platform. This replaces the ad-hoc logging approach with a centralized, consistent system that provides:

1. **Expert-specific log files** (`ExpertClass-expXX.log`)
2. **Prefixed console output** (`[ExpertClass-ID] message`)
3. **Automatic caching** (prevents logger recreation)
4. **Unified configuration** (respects platform STDOUT_LOGGING and FILE_LOGGING settings)

## Changes Made

### 1. Core Logger Module (`ba2_trade_platform/logger.py`)

Added new function `get_expert_logger(expert_class: str, expert_id: int)`:
- Creates loggers with format: `expert_class_expXX` (e.g., `tradingagents_exp5`)
- Log files: `ExpertClass-expXX.log` (e.g., `TradingAgents-exp5.log`)
- Console prefix: `[ExpertClass-ID]` (e.g., `[TradingAgents-5]`)
- Uses same formatter as main app logger
- Implements caching to avoid recreation
- Custom `ExpertFormatter` class prepends expert identifier to all messages

### 2. Updated All Expert Classes

**Updated Experts:**
- `FMPRating`
- `FinnHubRating`
- `FMPSenateTraderCopy`
- `FMPSenateTraderWeight`
- `TradingAgents`

**Changes Applied:**
1. Changed import: `from ...logger import logger` → `from ...logger import get_expert_logger`
2. Added in `__init__`: `self.logger = get_expert_logger("ExpertName", id)`
3. Replaced all `logger.` → `self.logger.`

### 3. Refactored TradingAgents Internal Logger

**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/logger.py`

- **Removed:** 400+ lines of custom logger implementation
- **Replaced with:** Simple wrapper around BA2 platform's `get_expert_logger()`
- **Retained:** All convenience functions for backwards compatibility:
  - `log_tool_call()`
  - `log_tool_result()`
  - `log_agent_start()`
  - `log_agent_complete()`
  - `log_step_start()`
  - `log_step_complete()`
- **Result:** DRY principle - no reinventing the wheel

## Benefits

### For Developers
- **Consistent API:** All experts use `self.logger.info()`, `.debug()`, etc.
- **No Boilerplate:** Just call `get_expert_logger()` in `__init__`
- **Easy Debugging:** Each expert instance has its own log file
- **Clean Console:** `[ExpertClass-ID]` prefix makes it easy to filter output

### For Operations
- **Trace Specific Instances:** Find logs for TradingAgents expert #5 in `TradingAgents-exp5.log`
- **Reduced Log Clutter:** Each expert's logs are isolated
- **Rotating Files:** 10MB max size, 7 backups (70MB total per expert instance)
- **UTF-8 Encoding:** Handles Unicode characters properly

### For Testing
- **Isolated Logs:** Each test can use different expert ID
- **Easy Verification:** Check specific log files for test output
- **No Interference:** Multiple experts can run simultaneously without log conflicts

## Usage Example

```python
from ba2_trade_platform.logger import get_expert_logger

class MyExpert(MarketExpertInterface):
    def __init__(self, id: int):
        super().__init__(id)
        self._load_expert_instance(id)
        
        # Initialize expert-specific logger
        self.logger = get_expert_logger("MyExpert", id)
    
    def run_analysis(self, market_analysis: MarketAnalysis) -> None:
        symbol = market_analysis.symbol
        self.logger.info(f"Starting analysis for {symbol}")
        
        try:
            # Do analysis...
            self.logger.debug(f"Fetched data for {symbol}")
            result = self._analyze()
            self.logger.info(f"Analysis complete: {result}")
        except Exception as e:
            self.logger.error(f"Analysis failed: {e}", exc_info=True)
```

**Console Output:**
```
2025-10-20 15:00:00 - myexpert_exp3 - module - INFO - [MyExpert-3] Starting analysis for AAPL
2025-10-20 15:00:01 - myexpert_exp3 - module - DEBUG - [MyExpert-3] Fetched data for AAPL
2025-10-20 15:00:02 - myexpert_exp3 - module - INFO - [MyExpert-3] Analysis complete: BUY
```

**Log File:** `logs/MyExpert-exp3.log` (same format)

## Log File Locations

- **Path:** `HOME_PARENT/logs/` (typically `~/Documents/ba2_trade_platform/logs/`)
- **Format:** `<ExpertClass>-exp<ID>.log`
- **Examples:**
  - `TradingAgents-exp1.log`
  - `FMPRating-exp6.log`
  - `FinnHubRating-exp2.log`
  - `FMPSenateTraderCopy-exp3.log`

## Backwards Compatibility

### TradingAgents Internal Code
All existing TradingAgents internal code continues to work:
- `logger.info()` → Routes to `get_expert_logger()`
- `log_agent_start()` → Works as before
- `log_tool_call()` → Works as before

### Legacy Code
If any code still uses the old `logger` import, it will use the main platform logger (still works, just not expert-specific).

## Testing

Run test script:
```bash
.venv\Scripts\python.exe test_files\test_expert_logger.py
```

Expected results:
- ✓ Console shows `[ExpertClass-ID]` prefix
- ✓ Log files created in `logs/` directory
- ✓ Logger caching works (same ID returns same object)
- ✓ No duplicate prefixes
- ✓ UTF-8 encoding works

## Migration Notes

### For New Experts
1. Import: `from ...logger import get_expert_logger`
2. In `__init__`: `self.logger = get_expert_logger("ClassName", id)`
3. Use: `self.logger.info()`, `.debug()`, `.warning()`, `.error()`

### For Existing Code
All existing experts have been updated. No action required.

### For Future Development
Always use `self.logger` instead of importing `logger` directly. This ensures proper expert-specific logging.

## Technical Details

### Logger Hierarchy
- Main logger: `ba2_trade_platform`
- Expert loggers: `<expertclass>_exp<id>` (e.g., `fmprating_exp1`)
- All set `propagate = False` to avoid duplicate logs

### Handler Configuration
Each expert logger has:
1. **Console Handler** (if STDOUT_LOGGING=True):
   - Level: DEBUG
   - Format: `ExpertFormatter` (adds prefix)
   - Output: stdout with UTF-8 encoding
   
2. **File Handler** (if FILE_LOGGING=True):
   - Level: DEBUG
   - Format: `ExpertFormatter` (adds prefix)
   - File: `logs/<ExpertClass>-exp<ID>.log`
   - Rotation: 10MB, 7 backups

### Formatter Details
- **Base Format:** `%(asctime)s - %(name)s - %(module)s - %(levelname)s - %(message)s`
- **Expert Prefix:** `[<ExpertClass>-<ID>]` prepended to message
- **Duplicate Prevention:** Checks if prefix already exists before adding

## Benefits Over Old System

| Aspect | Old System | New System |
|--------|-----------|-----------|
| **File Creation** | Manual per expert | Automatic via `get_expert_logger()` |
| **Console Prefix** | Inconsistent | Uniform `[Class-ID]` format |
| **Code Duplication** | High (TradingAgents had custom impl) | Low (unified function) |
| **Maintenance** | Update multiple files | Update one function |
| **Testing** | Complex (multiple loggers) | Simple (one test file) |
| **Debugging** | Mixed logs | Isolated per expert |

## Conclusion

The new expert logger system provides:
- ✅ **Consistency** across all experts
- ✅ **Simplicity** in implementation
- ✅ **Maintainability** through centralization
- ✅ **Debuggability** via expert-specific logs
- ✅ **Backwards compatibility** with existing code

All experts now use the same streamlined logging approach, eliminating the need to "reinvent the wheel" for each expert implementation.
