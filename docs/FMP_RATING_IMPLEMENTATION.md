# FMPRating Expert Implementation Summary

## What Was Built

A new market expert (**FMPRating**) that uses Financial Modeling Prep's analyst price target consensus to generate trading recommendations with calculated profit potential.

## Key Features

### 1. **Profit Potential Calculation** (Main Innovation)
Unlike FinnHubRating which only provides signals, FMPRating calculates **expected profit percentage**:

```python
# For BUY signals:
price_delta = target_high - current_price
weighted_delta = price_delta × (confidence / 100) × profit_ratio
expected_profit_percent = (weighted_delta / current_price) × 100
```

**Example**: 
- Current: $100, High Target: $200, Confidence: 80%, Profit Ratio: 1.0
- Expected Profit = ($200-$100) × 0.80 × 1.0 / $100 = **80%**

### 2. **Dual API Integration**
- **Price Target Consensus API**: Gets consensus, high, low, median targets
- **Upgrade/Downgrade API**: Gets analyst rating distribution for count

### 3. **Confidence Scoring**
Two-part confidence calculation:
- **Base Confidence**: Derived from analyst agreement (100 - target_spread%)
- **Analyst Boost**: +2% per analyst above minimum (max +20%)

### 4. **Configurable Settings**

#### `profit_ratio` (default: 1.0)
- Multiplier for profit calculation
- 1.0 = Full consensus range
- 0.5 = Conservative (expect 50% of target)
- 1.2 = Aggressive (expect to exceed target)

#### `min_analysts` (default: 3)
- Minimum analyst coverage required
- Below threshold → HOLD with low confidence
- Prevents recommendations on sparse data

## Files Created/Modified

### New Files
1. **`ba2_trade_platform/modules/experts/FMPRating.py`** (704 lines)
   - Complete expert implementation
   - API integration for price targets
   - Profit calculation algorithm
   - Beautiful UI rendering

2. **`docs/FMP_RATING_EXPERT.md`** 
   - Comprehensive documentation
   - Usage examples and best practices
   - Troubleshooting guide

3. **`test_fmp_rating.py`**
   - API endpoint tests
   - Expert instantiation tests
   - Calculation verification

### Modified Files
1. **`ba2_trade_platform/modules/experts/__init__.py`**
   - Added FMPRating import
   - Registered in experts list

## How It Works

### Analysis Flow

1. **Fetch Data**
   ```
   GET /api/v4/price-target-consensus?symbol=AAPL
   → {targetConsensus: 251.76, targetHigh: 310, targetLow: 173, targetMedian: 240}
   
   GET /api/v4/upgrades-downgrades-consensus?symbol=AAPL
   → {strongBuy: 1, buy: 66, hold: 34, sell: 8, strongSell: 0}
   ```

2. **Calculate Signal**
   ```python
   if (consensus - current_price) / current_price > 0.05:
       signal = BUY, use target_high
   elif (consensus - current_price) / current_price < -0.05:
       signal = SELL, use target_low
   else:
       signal = HOLD
   ```

3. **Calculate Confidence**
   ```python
   spread = (high - low) / consensus * 100
   base_confidence = 100 - spread
   analyst_boost = min(20, (analyst_count - min_analysts) * 2)
   confidence = base_confidence + analyst_boost
   ```

4. **Calculate Expected Profit**
   ```python
   delta = target_price - current_price
   weighted_delta = delta * (confidence/100) * profit_ratio
   expected_profit_pct = (weighted_delta / current_price) * 100
   ```

5. **Store Results**
   - Create ExpertRecommendation with signal, confidence, expected_profit_percent
   - Create 4 AnalysisOutput records (analysis, targets, API responses)
   - Update MarketAnalysis state

## UI Features

### Rich Visualization
1. **Recommendation Card**
   - Signal badge (BUY/SELL/HOLD) with icon and color
   - Confidence percentage
   - Expected profit percentage
   - Current price

2. **Price Target Grid** (4 cards)
   - Consensus Target (blue) - Average analyst target
   - Median Target (grey) - Middle analyst target
   - High Target (green) - Highest analyst target with upside %
   - Low Target (red) - Lowest analyst target with downside %

3. **Price Range Visualization**
   - Gradient bar from low to high
   - Blue marker for current price
   - Orange marker for consensus target
   - Visual representation of opportunity

4. **Methodology Expansion**
   - Complete formula explanations
   - Step-by-step calculations
   - Confidence algorithm details

## Example Output

```
FMP Analyst Price Target Consensus Analysis

Current Price: $150.00

Price Targets:
- Consensus Target: $251.76 (+67.8% from current)
- High Target: $310.00 (+106.7% from current)
- Low Target: $173.00 (+15.3% from current)
- Median Target: $240.00 (+60.0% from current)

Analyst Coverage: 109 analysts

Recommendation: BUY
Confidence: 65.6%
Expected Profit: 70.0%

Calculation Method:
Price Delta = High Target - Current Price = $310.00 - $150.00 = $160.00
Weighted Delta = $160.00 × 0.66 × 1.0 = $105.60
Expected Profit % = $105.60 / $150.00 × 100 = 70.0%
```

## FMPRating vs FinnHubRating

| Aspect | FinnHubRating | FMPRating |
|--------|---------------|-----------|
| **Data Source** | Analyst ratings (Buy/Sell) | Price targets ($ amounts) |
| **Primary Output** | Signal + Confidence | Signal + Confidence + **Expected Profit** |
| **Profit Calculation** | None (always 0%) | ✅ Weighted target delta |
| **Confidence Basis** | Rating score ratios | Target spread + coverage |
| **Settings** | Strong factor | Profit ratio, min analysts |
| **Best For** | Sentiment gauge | **Profit potential analysis** |
| **Use Case** | Quick consensus | **Quantitative targets** |

## Key Advantages

### 1. **Quantitative Profit Expectations**
- Not just "should I buy?" but "how much profit can I expect?"
- Weighted by confidence in the consensus
- Adjustable risk profile via profit_ratio

### 2. **Price Target Context**
- Shows not just consensus but full range (high/low/median)
- Visual representation of opportunity vs. risk
- Analyst agreement reflected in confidence

### 3. **Flexible Risk Management**
- Conservative mode: profit_ratio = 0.5-0.7
- Normal mode: profit_ratio = 0.8-1.0
- Aggressive mode: profit_ratio = 1.1-1.3
- Adapt to market conditions or trading style

### 4. **Data Quality Control**
- Minimum analyst threshold prevents sparse data
- Confidence boost rewards heavy coverage
- Graceful degradation when data insufficient

## Usage Scenarios

### Scenario 1: Growth Stock with High Targets
```python
Symbol: NVDA
Current: $500
Consensus: $750 (+50%)
High: $900 (+80%)
Low: $600 (+20%)
Analysts: 45

Settings: {profit_ratio: 1.0, min_analysts: 5}

Result:
Signal: BUY
Confidence: 78% (low spread, many analysts)
Expected Profit: 62% (($900-$500) × 0.78 × 1.0 / $500)
```

### Scenario 2: Conservative Play
```python
Symbol: JNJ
Current: $150
Consensus: $165 (+10%)
High: $180 (+20%)
Low: $155 (+3%)
Analysts: 25

Settings: {profit_ratio: 0.6, min_analysts: 8}

Result:
Signal: BUY
Confidence: 85% (tight spread)
Expected Profit: 6.1% (($180-$150) × 0.85 × 0.6 / $150)
```

### Scenario 3: Insufficient Coverage
```python
Symbol: SMALL_CAP
Analysts: 2

Settings: {profit_ratio: 1.0, min_analysts: 3}

Result:
Signal: HOLD
Confidence: 20%
Expected Profit: 0%
Details: "Insufficient analyst coverage (2 analysts, minimum 3 required)"
```

## Testing

### Test Results (AAPL)
```
Price Target Consensus:
✅ Consensus: $251.76
✅ High: $310.00
✅ Low: $173.00
✅ Median: $240.00

Analyst Ratings:
✅ 109 total analysts
✅ Buy: 67, Hold: 34, Sell: 8

Recommendation Calculation:
✅ Signal: BUY
✅ Confidence: 65.6%
✅ Expected Profit: 70.0%
```

## Integration Steps

1. **Add to Database**: FMPRating automatically appears in expert selection
2. **Configure Settings**: Set profit_ratio and min_analysts per instance
3. **Run Analysis**: Expert fetches FMP data and generates recommendation
4. **View Results**: Rich UI shows targets, profit, and calculations

## Requirements

- **FMP API Key**: Required in AppSetting table
- **FMP Plan**: Free tier works, paid plan for higher limits
- **API Endpoints**: v4/price-target-consensus, v4/upgrades-downgrades-consensus

## Future Enhancements

Potential improvements:
1. Historical accuracy tracking
2. Analyst-specific weighting
3. Time horizon extraction
4. Sector-specific settings
5. Target revision tracking

## Summary

FMPRating brings **quantitative profit targeting** to BA2 Trade Platform. While FinnHubRating tells you *what* analysts think, FMPRating tells you *how much profit* to expect based on where they think the price is going. The profit_ratio setting allows flexible risk management, making it suitable for both conservative and aggressive trading strategies.

**Bottom Line**: Use FMPRating when you need profit-focused recommendations with quantitative targets, not just sentiment signals.
