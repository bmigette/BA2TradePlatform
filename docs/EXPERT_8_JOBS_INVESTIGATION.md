# Expert 8 Scheduled Jobs - Investigation Report

## Issue
No scheduled jobs were being created for expert 8 (FMPSenateTraderCopy) even though it uses `instrument_selection_method: expert`.

## Root Cause
**The code is working correctly!** Jobs ARE being created properly. The issue is that the **running application needs to be restarted** to load the code changes.

## Test Results

### Test 1: Instrument Selection Logic ✅
```
Testing Expert 8 - FMPSenateTraderCopy
============================================================

✅ Expert loaded: FMPSenateTraderCopy
   Expert ID: 8

📋 Settings:
   instrument_selection_method: expert
   should_expand_instrument_jobs: false

🔧 Expert Properties:
   can_recommend_instruments: True
   should_expand_instrument_jobs: False

🧪 Testing _get_enabled_instruments logic:
   instrument_selection_method = 'expert'
   can_recommend_instruments = True
   ✅ Should return ['EXPERT']

📊 Result: ['EXPERT']
```

### Test 2: JobManager Scheduling ✅
```
🧪 Testing _get_enabled_instruments(8)...
   ✅ Returned: ['EXPERT']

🧪 Testing _schedule_expert_jobs...
   ✅ Method completed

📊 Checking scheduled jobs...
   Total jobs: 2
   Expert 8 jobs: 2

   📋 Expert 8 Jobs:
      - expert_8_symbol_EXPERT_subtype_AnalysisUseCase.ENTER_MARKET
        Next run: 2025-10-14 09:30:00+02:00
      - expert_8_symbol_EXPERT_subtype_AnalysisUseCase.OPEN_POSITIONS
        Next run: 2025-10-14 14:30:00+02:00
```

## Verification

The implementation is working correctly:

1. ✅ Expert 8 has `instrument_selection_method: expert` set
2. ✅ Expert 8 has `can_recommend_instruments: True` in its properties
3. ✅ `_get_enabled_instruments()` correctly returns `['EXPERT']`
4. ✅ `_schedule_expert_jobs()` creates 2 jobs with the EXPERT symbol:
   - ENTER_MARKET job scheduled for 09:30 (Mon-Fri)
   - OPEN_POSITIONS job scheduled for 14:30 (Mon, Tue, Thu)

## Solution

**Restart the main application** to load the updated code:

```powershell
# Stop the running application (if running in debug mode, stop the debugger)
# Then restart:
.venv\Scripts\python.exe main.py
```

Or if running in VS Code debugger, simply restart the debug session (F5).

## Expected Behavior After Restart

Once the application is restarted with the updated code:

1. JobManager will start during initialization
2. `_schedule_all_expert_jobs()` will be called
3. For expert 8:
   - `_get_enabled_instruments(8)` will return `['EXPERT']`
   - Two jobs will be created with the EXPERT symbol
4. Jobs will appear in the UI and execute at scheduled times
5. When jobs execute:
   - If `should_expand_instrument_jobs=False` (current setting): EXPERT symbol passed directly to expert
   - Expert handles the EXPERT symbol and makes decisions internally

## Code Changes That Fixed This

The fix was implemented in `ba2_trade_platform/core/JobManager.py` in the `_get_enabled_instruments()` method:

```python
# Check for special instrument selection methods - always create jobs with special symbols
if instrument_selection_method == 'expert' and can_recommend_instruments:
    # Expert-driven selection - create job with EXPERT symbol
    # At execution time, JobManager will check should_expand_instrument_jobs to decide whether to:
    # - expand into individual instrument jobs (if True)
    # - pass EXPERT symbol directly to expert (if False)
    logger.info(f"Expert {instance_id} uses expert-driven instrument selection - creating EXPERT job")
    return ["EXPERT"]
```

And in `_execute_expert_driven_analysis()`:

```python
should_expand = expert_properties.get('should_expand_instrument_jobs', True)

if not should_expand:
    # Case A: Pass EXPERT symbol directly to expert
    logger.info("Executing analysis with EXPERT symbol")
    self.submit_market_analysis(
        expert_instance_id=expert_instance_id,
        symbol="EXPERT",
        subtype=subtype
    )
    return
```

## Confirmation Steps

After restarting the application, you can confirm it's working by:

1. Opening the web UI
2. Navigate to the scheduled jobs view (if available)
3. Look for jobs with IDs like:
   - `expert_8_symbol_EXPERT_subtype_AnalysisUseCase.ENTER_MARKET`
   - `expert_8_symbol_EXPERT_subtype_AnalysisUseCase.OPEN_POSITIONS`

Or check the logs for messages like:
```
Expert 8 uses expert-driven instrument selection - creating EXPERT job
Scheduled job created: expert_8_symbol_EXPERT_subtype_AnalysisUseCase.ENTER_MARKET
```
