# FinnHubRating Expert

## Overview

`FinnHubRating` is a simple yet effective market expert that leverages Finnhub's analyst recommendation trends API to generate trading recommendations. It aggregates professional analyst ratings (Strong Buy, Buy, Hold, Sell, Strong Sell) and calculates weighted confidence scores.

## Key Features

- **Simple Integration**: Uses Finnhub's free/paid API for analyst recommendations
- **Weighted Scoring**: Strong buy/sell ratings are weighted higher using a configurable multiplier
- **Confidence Calculation**: Provides percentage confidence based on analyst consensus
- **Beautiful UI**: Visual bar charts and detailed breakdowns of analyst ratings
- **Medium-term Focus**: All recommendations are medium-risk, medium-term by design

## How It Works

### 1. Data Source

The expert fetches recommendation trends from Finnhub's API:
```
GET https://finnhub.io/api/v1/stock/recommendation?symbol=AAPL&token=YOUR_API_KEY
```

Response includes recent analyst ratings with counts for:
- Strong Buy
- Buy
- Hold
- Sell
- Strong Sell

### 2. Confidence Calculation

The confidence score is calculated using weighted scoring:

```python
buy_score = (strong_buy * strong_factor) + buy
hold_score = hold
sell_score = (strong_sell * strong_factor) + sell
total_weighted = buy_score + hold_score + sell_score

confidence = dominant_score / total_weighted
```

**Example:**
- Strong Buy: 10, Buy: 5, Hold: 3, Sell: 2, Strong Sell: 1
- Strong Factor: 2.0

```
Buy Score = (10 × 2.0) + 5 = 25
Hold Score = 3
Sell Score = (1 × 2.0) + 2 = 4
Total = 25 + 3 + 4 = 32
Confidence = 25 / 32 = 78.1%
```

### 3. Signal Generation

Recommendation logic:
- **BUY**: Buy Score > Sell Score AND Buy Score > Hold Score
- **SELL**: Sell Score > Buy Score AND Sell Score > Hold Score
- **HOLD**: Otherwise (Hold Score is highest or tied)

## Configuration

### Settings

#### Strong Factor (Default: 2.0)
Weight multiplier for strong buy/sell ratings. Higher values give more importance to strong ratings.

- **Range**: 1.0 - 5.0 (recommended)
- **Low (1.0-1.5)**: Equal weight for all ratings
- **Medium (2.0-2.5)**: Moderate emphasis on strong ratings (recommended)
- **High (3.0-5.0)**: Heavy emphasis on strong analyst convictions

**Impact Example:**
```
Ratings: Strong Buy: 5, Buy: 5, Hold: 5, Sell: 5, Strong Sell: 5

Strong Factor 1.0:
  Buy Score = 10, Hold Score = 5, Sell Score = 10 → HOLD (scores tied, hold loses)

Strong Factor 2.0:
  Buy Score = 15, Hold Score = 5, Sell Score = 15 → HOLD (scores tied, hold loses)

Strong Factor 3.0:
  Buy Score = 20, Hold Score = 5, Sell Score = 20 → HOLD (scores tied, hold loses)

But if Strong Buy: 10, Strong Sell: 2:
  Factor 2.0: Buy = 25, Hold = 5, Sell = 9 → BUY (64.1% confidence)
  Factor 3.0: Buy = 35, Hold = 5, Sell = 11 → BUY (68.6% confidence)
```

## API Requirements

### Finnhub API Key

1. **Get API Key**: Sign up at https://finnhub.io
   - Free tier: 60 API calls/minute
   - Pro tier: Higher limits for more symbols

2. **Configure in Platform**:
   - Navigate to Settings → Global Settings
   - Enter API key in "Finnhub API Key" field
   - Save settings

### API Endpoint Used

```
Endpoint: /stock/recommendation
Method: GET
Parameters:
  - symbol: Stock ticker (e.g., "AAPL", "NVDA")
  - token: Your API key

Rate Limits:
  - Free: 60 calls/minute
  - Premium: 300+ calls/minute
```

## Usage

### 1. Setup Expert Instance

1. Go to **Settings → Account Settings**
2. Click "Create Expert Instance"
3. Select account and expert type "FinnHubRating"
4. Configure settings:
   - **Strong Factor**: 2.0 (default) or adjust based on preference
5. Save instance

### 2. Enable Instruments

1. Select your FinnHubRating expert instance
2. Go to enabled instruments section
3. Add symbols you want to analyze (e.g., AAPL, NVDA, MSFT)
4. Save configuration

### 3. Run Analysis

#### Manual Analysis
1. Go to **Market Analysis** page
2. Select "Manual Analysis" tab
3. Choose FinnHubRating expert instance
4. Select symbols
5. Click "Submit Analysis"

#### Scheduled Analysis
1. Go to **Market Analysis** page
2. Select "Scheduled Jobs" tab
3. Create schedule for FinnHubRating expert
4. Configure frequency (daily, weekly, etc.)
5. Analysis runs automatically

### 4. View Results

Results display:
- **Recommendation**: BUY/SELL/HOLD with colored icon
- **Confidence**: Percentage based on analyst consensus
- **Rating Breakdown**: Visual bar chart of all ratings
- **Weighted Scores**: Buy score, sell score, hold count
- **Methodology**: Expandable calculation details

## Output Format

### ExpertRecommendation Record

```python
{
    "recommended_action": "BUY",  # OrderRecommendation enum
    "confidence": 0.781,          # 78.1%
    "risk_level": "MEDIUM",       # Always medium
    "time_horizon": "MEDIUM_TERM", # Always medium-term
    "expected_profit_percent": 0.0, # Not provided by Finnhub
    "price_at_date": 150.25,      # Current market price
    "details": "Full calculation breakdown..."
}
```

### Market Analysis State

```json
{
  "finnhub_rating": {
    "recommendation": {
      "signal": "BUY",
      "confidence": 0.781,
      "buy_score": 25.0,
      "hold_score": 3.0,
      "sell_score": 4.0,
      "period": "2025-10-01",
      "details": "Full analysis text..."
    },
    "api_response": [...],
    "settings": {
      "strong_factor": 2.0
    },
    "current_price": 150.25,
    "analysis_timestamp": "2025-10-01T12:00:00Z"
  }
}
```

## UI Rendering

The expert provides beautiful visualizations:

### 1. Header Section
- Expert name and symbol
- Current analysis status
- Timestamp

### 2. Recommendation Card
- Large signal indicator (BUY/SELL/HOLD)
- Confidence percentage
- Current price

### 3. Ratings Breakdown
- Visual bar charts for each rating category
- Color-coded (green for buy, red for sell, grey for hold)
- Count and percentage for each rating

### 4. Weighted Scores
- Three-column grid showing:
  - Buy Score (green)
  - Hold Score (grey)
  - Sell Score (red)

### 5. Methodology Expansion
- Detailed calculation explanation
- Formula breakdown
- Recommendation logic

## Comparison with Other Experts

| Feature | FinnHubRating | TradingAgents |
|---------|---------------|---------------|
| **Complexity** | Simple | Complex |
| **Data Source** | Analyst ratings | Multi-source AI |
| **Analysis Time** | < 1 second | 1-3 minutes |
| **API Dependency** | Finnhub only | Multiple APIs |
| **Configurability** | 1 setting | 20+ settings |
| **Use Case** | Quick consensus check | Deep analysis |
| **Best For** | High-frequency checks | Strategic decisions |

## Best Practices

### 1. Complement with Other Experts
- Use FinnHubRating for quick analyst sentiment
- Combine with TradingAgents for comprehensive analysis
- Compare recommendations across experts

### 2. Strong Factor Tuning
- Start with default 2.0
- Increase to 2.5-3.0 if you trust strong convictions
- Decrease to 1.5 if you prefer equal weighting

### 3. Instrument Selection
- Works best with large-cap stocks (more analyst coverage)
- May have limited data for small-cap or exotic instruments
- Check Finnhub's coverage before enabling symbols

### 4. Frequency
- Daily analysis recommended for active trading
- Weekly for longer-term positions
- Real-time for market hours decision making

### 5. API Rate Limits
- Monitor your Finnhub API usage
- Free tier: ~60 symbols per minute
- Spread analysis across multiple intervals if needed

## Error Handling

The expert handles errors gracefully:

### No API Key
```
Error: Cannot fetch recommendations: Finnhub API key not configured
Action: Configure API key in Global Settings
```

### API Error
```
Error: Failed to fetch Finnhub recommendations: 401 Unauthorized
Action: Verify API key is valid
```

### No Data
```
Signal: HOLD
Confidence: 0%
Details: No recommendation data available
Action: Symbol may not be covered by analysts
```

### Network Error
```
Error: Connection timeout
Action: Retry analysis, check network connectivity
```

## Troubleshooting

### Issue: Always getting HOLD
**Cause**: No analyst data for symbol
**Solution**: Check if symbol is covered at https://finnhub.io/dashboard

### Issue: Low confidence scores
**Cause**: Analyst ratings are evenly distributed
**Solution**: This is normal for controversial stocks, consider increasing strong_factor

### Issue: API rate limit exceeded
**Cause**: Too many requests in short time
**Solution**: Reduce frequency or upgrade Finnhub plan

### Issue: Recommendations not updating
**Cause**: Cached data or API issues
**Solution**: Re-run analysis, check Finnhub API status

## Technical Details

### File Structure
```
ba2_trade_platform/
  modules/
    experts/
      FinnHubRating.py     # Main expert implementation
      __init__.py          # Expert registration
```

### Dependencies
- `requests`: HTTP client for API calls
- `finnhub-python` (optional): Official Finnhub client

### Database Models Used
- `ExpertInstance`: Expert configuration
- `ExpertSetting`: Strong factor setting
- `MarketAnalysis`: Analysis container
- `ExpertRecommendation`: Trading recommendation
- `AnalysisOutput`: Detailed results and API responses

### API Client
Currently uses `requests` directly. Could be upgraded to use `finnhub-python` official client:
```python
import finnhub

finnhub_client = finnhub.Client(api_key=self._api_key)
recommendations = finnhub_client.recommendation_trends(symbol)
```

## Future Enhancements

### Potential Improvements

1. **Historical Trending**
   - Track analyst sentiment changes over time
   - Alert on significant rating shifts
   - Chart confidence trends

2. **Target Price Integration**
   - Use Finnhub's price target API
   - Calculate expected profit from analyst targets
   - Compare current price to consensus target

3. **Multi-timeframe Analysis**
   - Compare current vs 1-month-ago ratings
   - Detect improving/deteriorating sentiment
   - Weight recent changes higher

4. **Analyst Filtering**
   - Track individual analyst accuracy
   - Weight ratings by analyst reputation
   - Filter out low-accuracy analysts

5. **Sector Comparison**
   - Compare symbol's ratings to sector average
   - Relative strength scoring
   - Sector rotation signals

## License & Attribution

This expert is part of the BA2 Trade Platform and follows the project's license.

**Data Source**: Finnhub.io (https://finnhub.io)
- Free tier available
- Attribution required for free tier usage
- Commercial license available

## Support

For issues or questions:
1. Check logs in `ba2_trade_platform/logs/app.log`
2. Verify Finnhub API key is configured
3. Test API access at https://finnhub.io/dashboard
4. Review this documentation

## References

- [Finnhub API Documentation](https://finnhub.io/docs/api/recommendation-trends)
- [BA2 Trade Platform Documentation](../../../README.md)
- [Market Expert Interface](../../core/MarketExpertInterface.py)
