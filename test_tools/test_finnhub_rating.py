"""
Test script for FinnHubRating expert
"""
import sys
from pathlib import Path

# Add parent directory to path for imports
parent_dir = Path(__file__).parent.parent
sys.path.insert(0, str(parent_dir))

from ba2_trade_platform.modules.experts.FinnHubRating import FinnHubRating
from ba2_trade_platform.core.models import ExpertInstance, MarketAnalysis
from ba2_trade_platform.core.db import get_instance, add_instance
from ba2_trade_platform.core.types import MarketAnalysisStatus, AnalysisUseCase, OrderRecommendation
from ba2_trade_platform.logger import logger

def test_finnhub_rating():
    """Test FinnHubRating expert functionality."""
    
    print("=" * 80)
    print("Testing FinnHubRating Expert")
    print("=" * 80)
    
    # Test 1: Check expert description
    print("\n1. Testing expert description...")
    description = FinnHubRating.description()
    print(f"   Description: {description}")
    assert description, "Description should not be empty"
    print("   ✓ Description test passed")
    
    # Test 2: Check settings definitions
    print("\n2. Testing settings definitions...")
    settings = FinnHubRating.get_settings_definitions()
    print(f"   Settings: {list(settings.keys())}")
    assert 'strong_factor' in settings, "Should have strong_factor setting"
    assert settings['strong_factor']['default'] == 2.0, "Default strong_factor should be 2.0"
    print("   ✓ Settings test passed")
    
    # Test 3: Test recommendation calculation
    print("\n3. Testing recommendation calculation...")
    
    # Mock trends data
    trends_data = [{
        'period': '2025-10-01',
        'strongBuy': 10,
        'buy': 5,
        'hold': 3,
        'sell': 2,
        'strongSell': 1
    }]
    
    # Create a temporary instance for testing (won't save to DB)
    class MockExpert:
        def __init__(self):
            pass
        
        def _calculate_recommendation(self, trends_data, strong_factor):
            return FinnHubRating._calculate_recommendation(None, trends_data, strong_factor)
    
    expert = MockExpert()
    
    # Test with strong_factor = 2.0
    result = expert._calculate_recommendation(trends_data, 2.0)
    
    print(f"   Signal: {result['signal']}")
    print(f"   Confidence: {result['confidence']:.1%}")
    print(f"   Buy Score: {result['buy_score']}")
    print(f"   Hold Score: {result['hold_score']}")
    print(f"   Sell Score: {result['sell_score']}")
    
    # Calculate expected values
    # Buy Score = (10 * 2.0) + 5 = 25
    # Hold Score = 3
    # Sell Score = (1 * 2.0) + 2 = 4
    # Total = 25 + 3 + 4 = 32
    # Confidence = 25 / 32 = 0.78125 (78.1%)
    
    assert result['buy_score'] == 25.0, f"Buy score should be 25.0, got {result['buy_score']}"
    assert result['hold_score'] == 3.0, f"Hold score should be 3.0, got {result['hold_score']}"
    assert result['sell_score'] == 4.0, f"Sell score should be 4.0, got {result['sell_score']}"
    assert abs(result['confidence'] - 0.78125) < 0.001, f"Confidence should be ~0.78125, got {result['confidence']}"
    print("   ✓ Calculation test passed")
    
    # Test 4: Test with different strong_factor
    print("\n4. Testing with strong_factor = 3.0...")
    result = expert._calculate_recommendation(trends_data, 3.0)
    
    # Buy Score = (10 * 3.0) + 5 = 35
    # Sell Score = (1 * 3.0) + 2 = 5
    # Total = 35 + 5 + 3 = 43
    # Confidence = 35 / 43 = 0.81395 (81.4%)
    
    print(f"   Buy Score: {result['buy_score']}")
    print(f"   Sell Score: {result['sell_score']}")
    print(f"   Confidence: {result['confidence']:.1%}")
    
    assert result['buy_score'] == 35.0, f"Buy score should be 35.0, got {result['buy_score']}"
    assert result['sell_score'] == 5.0, f"Sell score should be 5.0, got {result['sell_score']}"
    print("   ✓ Strong factor test passed")
    
    # Test 5: Test sell signal
    print("\n5. Testing sell signal...")
    trends_data_sell = [{
        'period': '2025-10-01',
        'strongBuy': 1,
        'buy': 2,
        'hold': 3,
        'sell': 5,
        'strongSell': 10
    }]
    
    result = expert._calculate_recommendation(trends_data_sell, 2.0)
    print(f"   Signal: {result['signal']}")
    print(f"   Confidence: {result['confidence']:.1%}")
    
    assert result['signal'] == OrderRecommendation.SELL, f"Should be SELL signal, got {result['signal']}"
    print("   ✓ Sell signal test passed")
    
    # Test 6: Test hold signal
    print("\n6. Testing hold signal...")
    trends_data_hold = [{
        'period': '2025-10-01',
        'strongBuy': 1,
        'buy': 1,
        'hold': 20,
        'sell': 1,
        'strongSell': 1
    }]
    
    result = expert._calculate_recommendation(trends_data_hold, 2.0)
    print(f"   Signal: {result['signal']}")
    print(f"   Confidence: {result['confidence']:.1%}")
    
    assert result['signal'] == OrderRecommendation.HOLD, f"Should be HOLD signal, got {result['signal']}"
    print("   ✓ Hold signal test passed")
    
    print("\n" + "=" * 80)
    print("All tests passed! ✓")
    print("=" * 80)
    
    print("\n" + "=" * 80)
    print("IMPORTANT SETUP INSTRUCTIONS:")
    print("=" * 80)
    print("""
To use FinnHubRating expert in the platform:

1. Configure Finnhub API Key:
   - Go to Settings → Global Settings in the web UI
   - Enter your Finnhub API key (get one free at https://finnhub.io)
   - Save settings

2. Create Expert Instance:
   - Go to Settings → Account Settings
   - Create a new expert instance of type "FinnHubRating"
   - Configure the "strong_factor" setting (default: 2.0)
   - Enable instruments you want to analyze

3. Run Analysis:
   - Go to Market Analysis page
   - Select your FinnHubRating expert instance
   - Choose symbols and run analysis

The expert will:
- Fetch analyst recommendation trends from Finnhub
- Calculate weighted buy/sell scores using your strong_factor
- Generate BUY/SELL/HOLD recommendations with confidence scores
- Display beautiful visualizations of analyst ratings
    """)
    print("=" * 80)

if __name__ == '__main__':
    try:
        test_finnhub_rating()
    except Exception as e:
        logger.error(f"Test failed: {e}", exc_info=True)
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
