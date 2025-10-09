"""
Test script for FMPRating expert
"""
import json
import requests
from ba2_trade_platform.config import get_app_setting

def test_price_target_consensus(symbol="AAPL"):
    """Test FMP price target consensus API endpoint."""
    api_key = get_app_setting('FMP_API_KEY')
    
    if not api_key:
        print("❌ FMP API key not found in settings")
        return
    
    print(f"\n{'='*60}")
    print(f"Testing Price Target Consensus API for {symbol}")
    print(f"{'='*60}\n")
    
    url = f"https://financialmodelingprep.com/api/v4/price-target-consensus"
    params = {
        "symbol": symbol,
        "apikey": api_key
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        print("✅ API Response received:")
        print(json.dumps(data, indent=2))
        
        # Extract key fields (API returns a list)
        if data and isinstance(data, list) and len(data) > 0:
            item = data[0]
            print(f"\n📊 Price Target Summary:")
            print(f"   Consensus: ${item.get('targetConsensus', 'N/A')}")
            print(f"   High: ${item.get('targetHigh', 'N/A')}")
            print(f"   Low: ${item.get('targetLow', 'N/A')}")
            print(f"   Median: ${item.get('targetMedian', 'N/A')}")
        
    except Exception as e:
        print(f"❌ Error: {e}")


def test_upgrade_downgrade(symbol="AAPL"):
    """Test FMP upgrade/downgrade consensus API endpoint."""
    api_key = get_app_setting('FMP_API_KEY')
    
    if not api_key:
        print("❌ FMP API key not found in settings")
        return
    
    print(f"\n{'='*60}")
    print(f"Testing Upgrade/Downgrade Consensus API for {symbol}")
    print(f"{'='*60}\n")
    
    url = f"https://financialmodelingprep.com/api/v4/upgrades-downgrades-consensus"
    params = {
        "symbol": symbol,
        "apikey": api_key
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        print("✅ API Response received:")
        print(json.dumps(data[:3] if isinstance(data, list) and len(data) > 3 else data, indent=2))
        print(f"\nTotal records: {len(data) if isinstance(data, list) else 1}")
        
    except Exception as e:
        print(f"❌ Error: {e}")


def test_fmp_rating_expert():
    """Test FMPRating expert instantiation and basic functionality."""
    from ba2_trade_platform.modules.experts import FMPRating
    from ba2_trade_platform.core.models import ExpertInstance, MarketAnalysis
    from ba2_trade_platform.core.db import add_instance, get_db
    from ba2_trade_platform.core.types import MarketAnalysisStatus
    
    print(f"\n{'='*60}")
    print("Testing FMPRating Expert Class")
    print(f"{'='*60}\n")
    
    session = get_db()
    
    try:
        # Check if we have a test account (ID 1 is usually the first account)
        print("1️⃣ Creating test ExpertInstance...")
        expert_instance = ExpertInstance(
            account_id=1,  # Assuming account 1 exists
            expert="FMPRating",
            settings={
                "profit_ratio": 1.0,
                "min_analysts": 3
            }
        )
        instance_id = add_instance(expert_instance)
        print(f"   ✅ Created ExpertInstance ID: {instance_id}")
        
        # Instantiate the expert
        print("\n2️⃣ Instantiating FMPRating expert...")
        expert = FMPRating(instance_id)
        print(f"   ✅ Expert instantiated successfully")
        print(f"   Description: {expert.description()}")
        
        # Check settings
        print("\n3️⃣ Checking settings definitions...")
        settings_def = FMPRating.get_settings_definitions()
        print(f"   ✅ Settings:")
        for key, value in settings_def.items():
            print(f"      - {key}: {value.get('description')}")
        
        # Test API methods
        print("\n4️⃣ Testing API data fetch...")
        consensus_data = expert._fetch_price_target_consensus("AAPL")
        if consensus_data:
            print(f"   ✅ Price target consensus fetched")
            print(f"      Consensus: ${consensus_data.get('targetConsensus', 'N/A')}")
        else:
            print(f"   ⚠️ No consensus data returned")
        
        upgrade_data = expert._fetch_upgrade_downgrade("AAPL")
        if upgrade_data:
            print(f"   ✅ Upgrade/downgrade data fetched ({len(upgrade_data)} records)")
        else:
            print(f"   ⚠️ No upgrade/downgrade data returned")
        
        # Test calculation (if we have data)
        if consensus_data:
            print("\n5️⃣ Testing recommendation calculation...")
            current_price = 150.0  # Mock price for testing
            recommendation = expert._calculate_recommendation(
                consensus_data, upgrade_data, current_price, 
                profit_ratio=1.0, min_analysts=3
            )
            print(f"   ✅ Recommendation calculated:")
            print(f"      Signal: {recommendation['signal'].value}")
            print(f"      Confidence: {recommendation['confidence']:.1f}%")
            print(f"      Expected Profit: {recommendation['expected_profit_percent']:.1f}%")
            print(f"      Analysts: {recommendation['analyst_count']}")
        
        print("\n✅ All tests passed!")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        session.close()


if __name__ == "__main__":
    # Test API endpoints
    test_price_target_consensus("AAPL")
    test_upgrade_downgrade("AAPL")
    
    # Test expert class
    test_fmp_rating_expert()
