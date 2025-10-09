# FMPRating Expert

## Overview

`FMPRating` is a sophisticated market expert that leverages Financial Modeling Prep's (FMP) analyst price target consensus data to generate trading recommendations. Unlike FinnHubRating which uses simple analyst ratings, FMPRating calculates expected profit potential based on analyst price targets weighted by confidence and a configurable profit ratio.

## Key Features

- **Price Target Analysis**: Uses consensus, high, low, and median analyst price targets
- **Profit Potential Calculation**: Calculates weighted expected profit based on target prices
- **Confidence Scoring**: Derives confidence from analyst agreement (target spread)
- **Analyst Coverage Threshold**: Requires minimum number of analysts for valid recommendations
- **Configurable Risk Profile**: Adjustable profit ratio for conservative/aggressive positioning
- **Rich UI Visualization**: Price range visualization, target breakdowns, and methodology explanations

## How It Works

### 1. Data Sources

The expert fetches data from two FMP API endpoints:

#### Price Target Consensus
```
GET https://financialmodelingprep.com/api/v4/price-target-consensus?symbol=AAPL
```

Response includes:
- `targetConsensus`: Average analyst price target
- `targetHigh`: Highest analyst price target
- `targetLow`: Lowest analyst price target
- `targetMedian`: Median analyst price target

#### Upgrade/Downgrade Consensus
```
GET https://financialmodelingprep.com/api/v4/upgrades-downgrades-consensus?symbol=AAPL
```

Response includes analyst rating distribution:
- `strongBuy`: Number of Strong Buy ratings
- `buy`: Number of Buy ratings
- `hold`: Number of Hold ratings
- `sell`: Number of Sell ratings
- `strongSell`: Number of Strong Sell ratings

### 2. Signal Determination

The recommendation signal is based on the consensus target vs. current price:

```python
price_delta_pct = ((target_consensus - current_price) / current_price) * 100

if price_delta_pct > 5:  # More than 5% upside
    signal = BUY
    target_price = target_high  # Use high target for BUY
elif price_delta_pct < -5:  # More than 5% downside
    signal = SELL
    target_price = target_low  # Use low target for SELL
else:
    signal = HOLD
    target_price = target_consensus
```

**Key Insight**: For BUY signals, we use the **high target** to calculate profit potential. For SELL signals, we use the **low target**. This provides a more realistic assessment of the opportunity.

### 3. Expected Profit Calculation

This is the core innovation of FMPRating - calculating weighted profit potential:

**For BUY signals:**
```python
price_delta = target_high - current_price
weighted_delta = price_delta × (confidence / 100) × profit_ratio
expected_profit_percent = (weighted_delta / current_price) × 100
```

**For SELL signals:**
```python
price_delta = current_price - target_low
weighted_delta = price_delta × (confidence / 100) × profit_ratio
expected_profit_percent = (weighted_delta / current_price) × 100
```

**Example (BUY scenario):**
- Current Price: $100
- High Target: $200
- Confidence: 80%
- Profit Ratio: 1.0

```
Price Delta = $200 - $100 = $100
Weighted Delta = $100 × 0.80 × 1.0 = $80
Expected Profit % = ($80 / $100) × 100 = 80%
```

This means we expect an 80% profit based on analyst consensus weighted by our confidence in that consensus.

### 4. Confidence Calculation

Confidence is calculated in two parts:

**Base Confidence** - derived from analyst agreement:
```python
consensus_spread_pct = ((target_high - target_low) / target_consensus) × 100
base_confidence = max(0, min(100, 100 - consensus_spread_pct))
```

- 0% spread (perfect agreement) = 100% confidence
- 50% spread = 50% confidence
- 100% spread = 0% confidence

**Analyst Boost** - reward for more analyst coverage:
```python
analyst_confidence_boost = min(20, (analyst_count - min_analysts) × 2)
```

- Each analyst above minimum adds +2% confidence
- Capped at +20% maximum boost

**Final Confidence:**
```python
confidence = min(100, base_confidence + analyst_confidence_boost)
```

### 5. Minimum Analysts Threshold

If analyst coverage is below the `min_analysts` setting (default 3):
- Recommendation defaults to HOLD
- Confidence set to 20% (low)
- Details explain insufficient coverage

This prevents recommendations based on sparse data.

## Configuration Settings

### `profit_ratio` (float, default: 1.0)

**Description**: Multiplier applied to the weighted price target delta

**Purpose**: Allows adjustment of conservative vs. aggressive positioning

**Examples**:
- `1.0` - Use full analyst consensus range (default)
- `0.5` - Conservative, expect only 50% of the price movement
- `0.8` - Moderately conservative
- `1.2` - Aggressive, expect to exceed analyst targets

**Use Cases**:
- Set lower (0.5-0.8) for volatile markets or uncertain conditions
- Set higher (1.0-1.2) for strong conviction or trend-following strategies
- Adjust based on historical accuracy of analysts for specific sectors

### `min_analysts` (int, default: 3)

**Description**: Minimum number of analysts required for valid recommendation

**Purpose**: Ensures recommendations are based on sufficient coverage

**Examples**:
- `3` - Minimum coverage (default, suitable for most stocks)
- `5` - Higher confidence threshold (for large-cap stocks)
- `10` - Very strict (for highly-covered mega-cap stocks)
- `1` - Accept single analyst (risky, only for niche stocks)

**Use Cases**:
- Large-cap stocks: Use 5-10 for better consensus
- Mid-cap stocks: Use 3-5
- Small-cap stocks: May need to accept 1-2 analysts
- Adjust based on typical coverage in your trading universe

## API Requirements

### FMP API Key

Required in AppSetting table:
```sql
INSERT INTO AppSetting (name, value) VALUES ('FMP_API_KEY', 'your_api_key_here');
```

Get a free API key from: https://site.financialmodelingprep.com/developer/docs

### API Endpoints Used

1. **Price Target Consensus** (v4)
   - Endpoint: `/api/v4/price-target-consensus`
   - Rate Limit: Varies by plan
   - Free Tier: Limited requests per day

2. **Upgrades/Downgrades Consensus** (v4)
   - Endpoint: `/api/v4/upgrades-downgrades-consensus`
   - Rate Limit: Varies by plan
   - Free Tier: Limited requests per day

## Database Storage

### ExpertRecommendation Fields

- `recommended_action`: BUY, SELL, or HOLD
- `expected_profit_percent`: Calculated weighted profit percentage
- `confidence`: 1-100 scale confidence score
- `price_at_date`: Current stock price at analysis time
- `details`: Full analysis text with calculations
- `risk_level`: Always MEDIUM
- `time_horizon`: Always MEDIUM_TERM

### AnalysisOutput Records

1. **FMP Price Target Analysis** (type: `fmp_rating_analysis`)
   - Full detailed analysis text with calculations

2. **Price Targets** (type: `price_targets`)
   - Structured price target summary for UI display
   - Consensus, High, Low, Median, Analyst Count

3. **FMP Consensus API Response** (type: `fmp_consensus_response`)
   - Raw JSON response from price target consensus API

4. **FMP Upgrade/Downgrade Data** (type: `fmp_upgrade_downgrade`)
   - Raw JSON response from upgrade/downgrade API

## UI Visualization

### Main Card Sections

1. **Recommendation Summary**
   - Signal badge (BUY/SELL/HOLD) with icon
   - Confidence percentage
   - Expected profit percentage
   - Current price

2. **Analyst Price Targets**
   - Four-card grid showing:
     - Consensus Target (blue)
     - Median Target (grey)
     - High Target (green) with upside %
     - Low Target (red) with downside %

3. **Price Range Visualization**
   - Gradient bar from low to high target
   - Current price marker (blue vertical line)
   - Target consensus marker (orange vertical line)
   - Visual representation of potential movement

4. **Analysis Settings**
   - Profit Ratio and Min Analysts display

5. **Calculation Methodology** (Expandable)
   - Formula explanations
   - Step-by-step calculations
   - Confidence algorithm details

## Comparison with FinnHubRating

| Feature | FinnHubRating | FMPRating |
|---------|---------------|-----------|
| **Data Source** | Analyst ratings (Strong Buy/Sell) | Price target consensus |
| **Primary Metric** | Rating distribution | Target prices |
| **Profit Calculation** | None (always 0%) | Weighted target delta |
| **Confidence Source** | Rating score ratios | Target spread + coverage |
| **Configurable Settings** | Strong factor | Profit ratio, min analysts |
| **Signal Logic** | Buy/Hold/Sell score comparison | Price target vs. current |
| **Best For** | Quick sentiment gauge | Profit potential analysis |

## Use Cases

### 1. Profit-Focused Trading

**Scenario**: You want to prioritize opportunities with highest profit potential

**Configuration**:
```python
{
    "profit_ratio": 1.0,
    "min_analysts": 5
}
```

**Strategy**: Only take recommendations with strong analyst consensus (5+ analysts) and full expected profit range.

### 2. Conservative Positioning

**Scenario**: You want safer recommendations with realistic profit expectations

**Configuration**:
```python
{
    "profit_ratio": 0.6,
    "min_analysts": 8
}
```

**Strategy**: Require heavy analyst coverage and expect only 60% of the analyst target movement.

### 3. Small-Cap Trading

**Scenario**: Trading small-cap stocks with limited analyst coverage

**Configuration**:
```python
{
    "profit_ratio": 0.8,
    "min_analysts": 2
}
```

**Strategy**: Accept lower coverage (2 analysts minimum) but use conservative profit ratio due to higher volatility.

### 4. Mega-Cap Growth

**Scenario**: Large-cap stocks with heavy coverage and steady trends

**Configuration**:
```python
{
    "profit_ratio": 1.2,
    "min_analysts": 10
}
```

**Strategy**: Require extensive coverage (10+ analysts) and expect to exceed consensus targets.

## Example Analysis Output

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
Weighted Delta = Price Delta × Confidence × Profit Ratio = $160.00 × 0.66 × 1.0 = $105.60
Expected Profit % = Weighted Delta / Current Price × 100 = $105.60 / $150.00 × 100 = 70.0%

Confidence Calculation:
Base Confidence = 100 - Target Spread % = 100 - 54.4% = 45.6%
Analyst Boost = min(20, (109 - 3) × 2) = 20.0%
Final Confidence = 65.6%
```

## Implementation Details

### File Location
```
ba2_trade_platform/modules/experts/FMPRating.py
```

### Class Structure
```python
class FMPRating(MarketExpertInterface):
    """FMP analyst price consensus expert"""
    
    # Key Methods:
    - __init__(id: int)
    - get_settings_definitions() -> Dict[str, Any]
    - run_analysis(symbol: str, market_analysis: MarketAnalysis)
    - render_market_analysis(market_analysis: MarketAnalysis)
    
    # Private Methods:
    - _fetch_price_target_consensus(symbol: str) -> Dict
    - _fetch_upgrade_downgrade(symbol: str) -> list
    - _calculate_recommendation(...) -> Dict
    - _create_expert_recommendation(...) -> int
    - _store_analysis_outputs(...)
    - _get_current_price(symbol: str) -> float
```

### Dependencies
- `requests`: HTTP requests to FMP API
- `json`: JSON serialization
- Core BA2 interfaces and models
- NiceGUI for rendering

## Best Practices

### 1. API Key Management
- Store FMP_API_KEY in AppSetting table (never hardcode)
- Monitor API usage to stay within rate limits
- Consider upgrading FMP plan for high-frequency trading

### 2. Setting Optimization
- Backtest different profit_ratio values for your trading style
- Adjust min_analysts based on stock universe (large-cap vs. small-cap)
- Review analyst accuracy periodically and adjust confidence accordingly

### 3. Integration with Other Experts
- Combine with FinnHubRating for complementary perspectives
- Use FMPRating for profit targeting, FinnHubRating for sentiment
- Consider averaging confidence scores across multiple experts

### 4. Error Handling
- Expert gracefully handles missing API data
- Defaults to HOLD with low confidence when insufficient data
- Logs all API failures for monitoring

### 5. Performance Monitoring
- Track actual vs. expected profit over time
- Adjust profit_ratio based on historical accuracy
- Monitor analyst accuracy by sector/symbol

## Future Enhancements

Potential improvements for FMPRating:

1. **Historical Accuracy Tracking**
   - Store past recommendations and outcomes
   - Calculate analyst accuracy by sector
   - Auto-adjust confidence based on historical performance

2. **Time Horizon Analysis**
   - Extract analyst time horizons from FMP data
   - Classify as short/medium/long term dynamically
   - Adjust expected profit based on time horizon

3. **Sector-Specific Settings**
   - Different profit_ratio for tech vs. utilities
   - Sector-based min_analysts thresholds
   - Industry-specific confidence adjustments

4. **Price Target Revision Tracking**
   - Monitor analyst target changes over time
   - Boost confidence for consistent upward revisions
   - Lower confidence for volatile target changes

5. **Individual Analyst Weighting**
   - Track accuracy of specific analysts
   - Weight consensus by analyst track record
   - Filter out consistently inaccurate analysts

## Troubleshooting

### No Recommendations Generated

**Symptoms**: Expert returns HOLD with low confidence

**Possible Causes**:
1. Insufficient analyst coverage (below min_analysts)
2. FMP API key missing or invalid
3. Symbol not covered by FMP analysts
4. API rate limit exceeded

**Solutions**:
- Lower min_analysts setting for small-cap stocks
- Verify FMP_API_KEY in AppSetting table
- Check FMP documentation for symbol coverage
- Upgrade FMP API plan or reduce analysis frequency

### Unrealistic Profit Expectations

**Symptoms**: Expected profit % seems too high or low

**Possible Causes**:
1. Profit ratio set too high/low
2. Analyst targets outdated or inaccurate
3. High volatility causing wide target spreads

**Solutions**:
- Adjust profit_ratio to more conservative value (0.6-0.8)
- Check analyst target dates in FMP data
- Increase min_analysts to require more consensus
- Review confidence calculation - low confidence = less reliable

### API Errors

**Symptoms**: Analysis fails with API request errors

**Possible Causes**:
1. Invalid FMP API key
2. Rate limit exceeded
3. Network connectivity issues
4. FMP API service disruption

**Solutions**:
- Verify API key in AppSetting table
- Check FMP account rate limits
- Implement retry logic with exponential backoff
- Monitor FMP service status page

## Summary

FMPRating provides a quantitative, profit-focused approach to trading recommendations based on professional analyst price targets. Its key strength is the expected profit calculation that weights analyst consensus by confidence and allows risk adjustment through the profit ratio setting. 

Best suited for:
- Traders focused on profit potential vs. just signals
- Portfolios requiring quantitative justification
- Strategies that benefit from analyst consensus data
- Medium-term position trading (weeks to months)

Combine with technical analysis and other experts for comprehensive decision-making.
