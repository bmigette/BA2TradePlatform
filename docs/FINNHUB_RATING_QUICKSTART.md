# FinnHubRating Expert - Quick Start Guide

## What is FinnHubRating?

A simple market expert that uses **Finnhub analyst recommendation trends** to generate BUY/SELL/HOLD signals with confidence scores. It aggregates professional analyst ratings and calculates weighted scores based on strong buy/sell convictions.

## 5-Minute Setup

### Step 1: Get Finnhub API Key (2 minutes)
1. Go to https://finnhub.io
2. Sign up for free account (60 API calls/minute)
3. Copy your API key from dashboard

### Step 2: Configure API Key (1 minute)
1. Start BA2 Trade Platform: `.venv\Scripts\python.exe main.py`
2. Open browser: http://localhost:8080
3. Navigate to **Settings → Global Settings**
4. Paste Finnhub API key
5. Click **Save**

### Step 3: Create Expert Instance (2 minutes)
1. Go to **Settings → Account Settings**
2. Click **Create Expert Instance**
3. Select your account
4. Choose expert type: **FinnHubRating**
5. Set **Strong Factor**: 2.0 (default is good)
6. Click **Save**

### Step 4: Enable Symbols
1. Select your FinnHubRating instance
2. Scroll to **Enabled Instruments**
3. Add symbols (e.g., AAPL, NVDA, MSFT)
4. Enable and set weights
5. Click **Save**

### Step 5: Run Analysis!
1. Go to **Market Analysis** page
2. Select **Manual Analysis** tab
3. Choose FinnHubRating expert
4. Select symbols
5. Click **Submit Analysis**
6. Wait a few seconds
7. View beautiful results!

## What You'll See

### Recommendation Display
```
┌─────────────────────────────────────┐
│  AAPL - Analyst Consensus           │
├─────────────────────────────────────┤
│  Recommendation: 📈 BUY             │
│  Confidence: 78.1%                  │
│  Current Price: $150.25             │
├─────────────────────────────────────┤
│  Analyst Ratings - 2025-10-01       │
│                                     │
│  Strong Buy    10  ████████████████ │
│  Buy            5  ███████          │
│  Hold           3  ████             │
│  Sell           2  ██               │
│  Strong Sell    1  █                │
│                                     │
│  Total Analysts: 21                 │
├─────────────────────────────────────┤
│  Weighted Scoring                   │
│  Strong Factor: 2.0x                │
│                                     │
│  Buy Score:  25.0                   │
│  Hold Score: 3.0                    │
│  Sell Score: 4.0                    │
└─────────────────────────────────────┘
```

## How Confidence is Calculated

**Simple Formula:**
```
Buy Score = (Strong Buy × Strong Factor) + Buy
Hold Score = Hold
Sell Score = (Strong Sell × Strong Factor) + Sell
Total = Buy Score + Hold Score + Sell Score
Confidence = Winning Score / Total
```

**Example:**
- 10 Strong Buy, 5 Buy, 3 Hold, 2 Sell, 1 Strong Sell
- Strong Factor = 2.0

```
Buy Score = (10 × 2.0) + 5 = 25
Hold Score = 3
Sell Score = (1 × 2.0) + 2 = 4
Total = 25 + 3 + 4 = 32
Confidence = 25 / 32 = 78.1%
→ BUY with 78.1% confidence
```

## Recommendation Logic

| Condition | Signal |
|-----------|--------|
| Buy Score > Sell Score AND Buy Score > Hold Score | **BUY** |
| Sell Score > Buy Score AND Sell Score > Hold Score | **SELL** |
| Otherwise (Hold Score is highest or tied) | **HOLD** |

## Settings Explained

### Strong Factor (Default: 2.0)
How much more weight to give "Strong Buy" and "Strong Sell" vs regular ratings.

- **1.0**: Equal weight (Strong Buy = Buy)
- **2.0**: Double weight (recommended)
- **3.0**: Triple weight (aggressive)
- **5.0**: 5x weight (very aggressive)

**When to increase:**
- You trust analyst strong convictions
- Looking for high-confidence signals
- Want to filter out weak consensus

**When to decrease:**
- Prefer equal weighting
- Don't trust strong ratings more
- Want more HOLD signals

## Best Use Cases

✅ **Great For:**
- Quick analyst sentiment check
- High-frequency signal generation
- Large-cap stocks with good analyst coverage
- Combining with other experts for confirmation
- Real-time market hours decisions

❌ **Not Ideal For:**
- Small-cap stocks (limited analyst coverage)
- Exotic instruments
- Detailed fundamental analysis
- When you need profit targets

## Comparison: FinnHubRating vs TradingAgents

| Feature | FinnHubRating | TradingAgents |
|---------|---------------|---------------|
| **Speed** | < 1 second | 1-3 minutes |
| **Complexity** | Simple | Complex |
| **Data** | Analyst ratings | News + Technical + Fundamental |
| **Settings** | 1 (strong factor) | 20+ settings |
| **Use Case** | Quick check | Deep analysis |
| **API Calls** | 1 per symbol | 10+ per symbol |

**Recommendation:** Use both!
- FinnHubRating for quick sentiment
- TradingAgents for detailed analysis
- Compare results for high-confidence trades

## Common Issues

### "No recommendation data available"
**Cause:** Symbol not covered by analysts  
**Fix:** Check https://finnhub.io/dashboard to verify coverage

### Always getting HOLD
**Cause:** Analyst ratings are evenly split  
**Fix:** Normal for controversial stocks, try increasing strong_factor

### API rate limit error
**Cause:** Too many requests (free tier: 60/min)  
**Fix:** Reduce frequency or upgrade Finnhub plan

### API key error
**Cause:** Invalid or missing API key  
**Fix:** Double-check key in Settings → Global Settings

## Testing Your Setup

Run the included test:
```powershell
.venv\Scripts\python.exe test_finnhub_rating.py
```

Should output:
```
✓ Description test passed
✓ Settings test passed
✓ Calculation test passed
✓ Strong factor test passed
✓ Sell signal test passed
✓ Hold signal test passed

All tests passed! ✓
```

## Next Steps

1. **Run your first analysis** on a well-known stock (AAPL, MSFT, NVDA)
2. **Experiment with strong_factor** values (try 1.5, 2.0, 3.0)
3. **Set up scheduled analysis** for daily updates
4. **Compare with TradingAgents** results
5. **Create trading rules** based on confidence thresholds

## Pro Tips

💡 **Tip 1:** Set up scheduled analysis to run every morning before market open

💡 **Tip 2:** Use confidence > 70% as a filter for high-conviction trades

💡 **Tip 3:** Track analyst sentiment changes over time to catch rating upgrades/downgrades

💡 **Tip 4:** Combine with your account's balance checks to ensure proper position sizing

💡 **Tip 5:** Enable multiple symbols at once for portfolio-wide sentiment analysis

## Documentation

- **Full Documentation:** `docs/FINNHUB_RATING_EXPERT.md`
- **Expert Implementation:** `ba2_trade_platform/modules/experts/FinnHubRating.py`
- **Test Script:** `test_finnhub_rating.py`
- **Finnhub API Docs:** https://finnhub.io/docs/api/recommendation-trends

## Support

Questions? Check:
1. Full documentation (link above)
2. Application logs: `ba2_trade_platform/logs/app.log`
3. Finnhub dashboard: https://finnhub.io/dashboard

---

**Ready to start?** Just follow the 5-minute setup above! 🚀
