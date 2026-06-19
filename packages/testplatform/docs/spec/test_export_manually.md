# Manual Testing Required for Export Feature

## What Was Implemented
- ✅ Frontend: Export CSV button added to dataset details page
- ✅ Backend: GET /api/datasets/:id/export endpoint created
- ✅ Code committed (commit: 0496f33)

## Issue
Multiple backend processes running on port 8002. The new /export endpoint isn't loading because old processes are handling requests.

## Solution for Next Session
1. Kill all backend processes:
   ```bash
   taskkill /F /IM python.exe /FI "WINDOWTITLE eq *uvicorn*"
   # Or manually find and kill PIDs: 50424, 47684, 3172, 58000, 44148
   ```

2. Restart backend cleanly:
   ```bash
   cd backend
   source venv/Scripts/activate  # or call venv\Scripts\activate.bat on Windows
   python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8002
   ```

3. Test export:
   - Navigate to http://localhost:5173/datasets/1
   - Click "Export CSV" button
   - Verify CSV file downloads with correct name (AAPL_1d_20260124_195119.csv)
   - Open CSV and verify data is correct

## Feature Test Steps (from feature_list.json)
1. Open dataset details page ✅
2. Click 'Export' button ✅ (button exists)
3. Select 'CSV' format from dropdown (N/A - direct export)
4. Click 'Download' button (combined with step 2)
5. Verify CSV file downloads (needs backend restart)
6. Open CSV and verify data is correct (needs backend restart)

## Status
- Frontend: ✅ Complete and tested visually
- Backend: ✅ Code complete, endpoint not loaded yet
- Integration: ⚠️ Needs backend restart to test
