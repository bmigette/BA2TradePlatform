# OpenAI API Usage Widget - 500 Error Diagnostics & Improvements

## Issue
The API usage widget is showing:
```
Error fetching usage data
OpenAI API error (500): { "error": { "message": "The server had an error processing your request...
```

## What Was Fixed

### 1. Enhanced 500 Error Handling
**Before:**
- Showed truncated error messages (100 chars)
- Generic "OpenAI API error (500)" message

**After:**
- Shows user-friendly message: "OpenAI server error (500) - their API may be experiencing issues. Try again later."
- Full error text still logged for debugging

### 2. Added Detailed Logging
Both async and sync versions now log:
- Request URL and parameters: `[OpenAI Usage] Calling https://api.openai.com/v1/organization/costs with start_time=... end_time=...`
- Response status: `[OpenAI Usage] Response status: 500`

This helps diagnose whether:
- The request is being sent correctly
- The API is responding
- The error is on OpenAI's side

### 3. Consistent Error Handling
- Async version: `_get_openai_usage_data_async()`
- Sync version: `_get_openai_usage_data()`
- Both now handle 500 errors identically

## How to Troubleshoot

### Step 1: Check the Logs
```bash
tail -f logs/app.debug.log | grep "\[OpenAI Usage\]"
```

You should see:
```
[OpenAI Usage] Calling https://api.openai.com/v1/organization/costs with start_time=1729...
[OpenAI Usage] Response status: 500
OpenAI API error 500: {"error": {"message": "The server..."}}
```

### Step 2: Check OpenAI Status
Visit https://status.openai.com/ to see if:
- ✅ All systems operational
- ⚠️ Service degradation
- 🔴 Major outage

### Step 3: Verify API Key
The 500 error could be caused by:
1. **Invalid admin key format** - Must start with `sk-admin`
2. **Expired API key** - Try regenerating
3. **Wrong key type** - Usage data requires Admin API Key, not regular API Key

### Step 4: Test the API Directly
```bash
# Using curl (replace with your admin key)
curl -H "Authorization: Bearer sk-admin-YOUR_KEY_HERE" \
  "https://api.openai.com/v1/organization/costs?start_time=$(date -d '30 days ago' +%s)&end_time=$(date +%s)"
```

Expected response:
- ✅ `{"data": [...]}` - Success (status 200)
- ⚠️ `{"error": {...}}` - Check error message
- 🔴 `500 Internal Server Error` - OpenAI service issue

## Possible Causes of 500 Error

### 1. OpenAI Service Issue (Most Likely)
- OpenAI's API experiencing temporary outage
- **Solution**: Wait and retry in a few minutes

### 2. Invalid Request Parameters
- Timestamps in wrong format
- Unsupported bucket_width value
- **Solution**: Check logs for exact parameters being sent

### 3. API Endpoint Change
- OpenAI changed endpoint format
- Deprecated parameters
- **Solution**: Check OpenAI API documentation for latest format

### 4. Rate Limiting
- Too many requests in short time
- **Solution**: Implement exponential backoff

### 5. Insufficient Permissions
- Regular API key used instead of Admin key
- **Solution**: Generate Admin API key at https://platform.openai.com/settings/organization/admin-keys

## Files Modified
- `ba2_trade_platform/ui/pages/overview.py`
  - Enhanced both `_get_openai_usage_data_async()` and `_get_openai_usage_data()`
  - Added debug logging for request parameters and response status
  - Improved error message for 500 responses
  - Applied same error handling to both functions

## Next Steps

### If 500 Error Persists
1. **Check logs**: Look for `[OpenAI Usage]` entries
2. **Check OpenAI status**: Visit status.openai.com
3. **Verify admin key**: Ensure using Admin API Key (`sk-admin-*`)
4. **Wait and retry**: OpenAI errors usually resolve quickly
5. **Try fallback**: Consider implementing fallback to basic cost estimate

### Future Improvements
- [ ] Add retry logic with exponential backoff
- [ ] Implement fallback to simpler OpenAI endpoint
- [ ] Add cache to reduce API calls
- [ ] Notify user about OpenAI service status
- [ ] Provide cost estimation without live API if service down

## Testing
To test the fix:
1. Open the app dashboard
2. Look at the API usage widget
3. If you see 500 error:
   - Check debug logs with `[OpenAI Usage]` filter
   - Verify you're using Admin API Key
   - Check OpenAI status page
   - Try refreshing page after 1-2 minutes

The enhanced logging will help identify if the issue is:
- ✅ Configuration (wrong API key)
- ✅ Network (connectivity issue)
- ✅ OpenAI (service issue)
- ✅ Request format (parameter issue)
