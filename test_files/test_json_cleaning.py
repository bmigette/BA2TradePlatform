"""
Test JSON cleaning function for TradingAgents summarization
This validates that the clean_json_string function correctly handles trailing commas
"""
import sys
import json
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.summarization.summarization import clean_json_string


def test_json_cleaning():
    """Test various JSON formatting issues"""
    
    print("Testing JSON cleaning function...")
    print("=" * 80)
    
    # Test 1: Trailing comma in array
    test1 = '''{
    "key_factors": [
        "Factor 1",
        "Factor 2",
        "Factor 3",
    ]
}'''
    
    print("\n1. Testing trailing comma in array:")
    print("   Input:", test1.replace('\n', ' '))
    cleaned1 = clean_json_string(test1)
    print("   Cleaned:", cleaned1.replace('\n', ' '))
    try:
        parsed1 = json.loads(cleaned1)
        print("   ✅ Valid JSON")
        print(f"   Parsed: {parsed1}")
    except json.JSONDecodeError as e:
        print(f"   ❌ Still invalid: {e}")
    
    # Test 2: Trailing comma in object
    test2 = '''{
    "symbol": "TSLA",
    "confidence": 72.0,
}'''
    
    print("\n2. Testing trailing comma in object:")
    print("   Input:", test2.replace('\n', ' '))
    cleaned2 = clean_json_string(test2)
    print("   Cleaned:", cleaned2.replace('\n', ' '))
    try:
        parsed2 = json.loads(cleaned2)
        print("   ✅ Valid JSON")
        print(f"   Parsed: {parsed2}")
    except json.JSONDecodeError as e:
        print(f"   ❌ Still invalid: {e}")
    
    # Test 3: Real-world example from error log (the exact failing JSON)
    test3 = '''{
    "symbol": "TSLA",
    "recommended_action": "SELL",
    "expected_profit_percent": 10.5,
    "price_at_date": 240.0,
    "confidence": 72.0,
    "details": "Technical analysis reveals distribution pattern with multiple rejections at $255 resistance.",
    "risk_level": "HIGH",
    "time_horizon": "SHORT_TERM",
    "key_factors": [
        "Multiple technical rejections at upper Bollinger Band resistance",
        "Margin compression from 29% to 18% in automotive segment",
        "Model 2 delays ceding mass-market to competitors",
    ],
    "stop_loss": 255.0,
    "take_profit": 215.0,
    "analysis_summary": {
        "market_trend": "BEARISH",
        "fundamental_strength": "MODERATE",
        "sentiment_score": 45.0,
        "macro_environment": "UNFAVORABLE",
        "technical_signals": "SELL"
    }
}'''
    
    print("\n3. Testing real-world example from error log:")
    print("   Input (first 100 chars):", test3[:100].replace('\n', ' '))
    cleaned3 = clean_json_string(test3)
    print("   Cleaned (first 100 chars):", cleaned3[:100].replace('\n', ' '))
    try:
        parsed3 = json.loads(cleaned3)
        print("   ✅ Valid JSON")
        print(f"   Parsed symbol: {parsed3['symbol']}")
        print(f"   Parsed action: {parsed3['recommended_action']}")
        print(f"   Key factors count: {len(parsed3['key_factors'])}")
    except json.JSONDecodeError as e:
        print(f"   ❌ Still invalid: {e}")
    
    # Test 4: Multiple trailing commas
    test4 = '''{
    "array": ["a", "b",],
    "nested": {
        "items": [1, 2, 3,],
        "value": 100,
    },
}'''
    
    print("\n4. Testing multiple trailing commas:")
    print("   Input:", test4.replace('\n', ' '))
    cleaned4 = clean_json_string(test4)
    print("   Cleaned:", cleaned4.replace('\n', ' '))
    try:
        parsed4 = json.loads(cleaned4)
        print("   ✅ Valid JSON")
        print(f"   Parsed: {parsed4}")
    except json.JSONDecodeError as e:
        print(f"   ❌ Still invalid: {e}")
    
    # Test 5: Comments (should be removed)
    test5 = '''{
    "value": 100,  // this is a comment
    "items": ["a", "b"]  // another comment
}'''
    
    print("\n5. Testing comment removal:")
    print("   Input:", test5.replace('\n', ' '))
    cleaned5 = clean_json_string(test5)
    print("   Cleaned:", cleaned5.replace('\n', ' '))
    try:
        parsed5 = json.loads(cleaned5)
        print("   ✅ Valid JSON")
        print(f"   Parsed: {parsed5}")
    except json.JSONDecodeError as e:
        print(f"   ❌ Still invalid: {e}")
    
    print("\n" + "=" * 80)
    print("✅ All tests completed!")


if __name__ == "__main__":
    test_json_cleaning()
