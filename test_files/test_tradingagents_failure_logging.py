"""
Test TradingAgents analysis failure activity logging
This validates that JSON parsing errors are logged to ActivityLog
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.summarization.summarization import _log_analysis_failure
from ba2_trade_platform.core.db import get_db, get_instance
from ba2_trade_platform.core.models import ActivityLog, MarketAnalysis, ExpertInstance
from ba2_trade_platform.core.types import ActivityLogType, ActivityLogSeverity


def test_analysis_failure_logging():
    """Test that analysis failures are logged to ActivityLog"""
    
    print("Testing TradingAgents analysis failure logging...")
    print("=" * 80)
    
    session = get_db()
    
    # Get first available expert instance for testing
    expert_instances = session.query(ExpertInstance).limit(1).all()
    if not expert_instances:
        print("❌ No expert instances found - cannot test")
        return
    
    expert_instance = expert_instances[0]
    print(f"Using ExpertInstance #{expert_instance.id} (Account #{expert_instance.account_id})")
    
    # Get or create a test MarketAnalysis
    market_analyses = session.query(MarketAnalysis).filter(
        MarketAnalysis.expert_instance_id == expert_instance.id
    ).limit(1).all()
    
    if not market_analyses:
        print("❌ No market analyses found - cannot test")
        return
    
    market_analysis = market_analyses[0]
    print(f"Using MarketAnalysis #{market_analysis.id} for symbol {market_analysis.symbol}")
    
    # Count existing failure logs
    existing_logs = session.query(ActivityLog).filter(
        ActivityLog.type == ActivityLogType.ANALYSIS_FAILED,
        ActivityLog.source_expert_id == expert_instance.id
    ).count()
    print(f"Existing failure logs: {existing_logs}")
    
    # Test 1: JSON parsing error
    print("\n1. Testing JSON parsing error logging:")
    test_state = {
        "market_analysis_id": market_analysis.id,
        "company_of_interest": market_analysis.symbol,
        "current_price": 100.0
    }
    
    error_msg = "Invalid json output: {\"key_factors\": [\"item1\", \"item2\",]} - Expecting value: line 14 column 5"
    
    print(f"   Simulating error: {error_msg[:80]}...")
    _log_analysis_failure(test_state, market_analysis.symbol, error_msg)
    
    # Check if log was created
    new_logs = session.query(ActivityLog).filter(
        ActivityLog.type == ActivityLogType.ANALYSIS_FAILED,
        ActivityLog.source_expert_id == expert_instance.id
    ).order_by(ActivityLog.created_at.desc()).first()
    
    if new_logs:
        print("   ✅ Activity log created!")
        print(f"   Log ID: {new_logs.id}")
        print(f"   Severity: {new_logs.severity}")
        print(f"   Description: {new_logs.description}")
        print(f"   Error type: {new_logs.data.get('error_type')}")
        print(f"   Fallback action: {new_logs.data.get('fallback_action')}")
        print(f"   Account ID: {new_logs.source_account_id}")
        print(f"   Expert ID: {new_logs.source_expert_id}")
    else:
        print("   ❌ No activity log found!")
    
    # Test 2: Generic error
    print("\n2. Testing generic analysis error logging:")
    error_msg2 = "LLM timeout - model failed to respond within 60 seconds"
    
    print(f"   Simulating error: {error_msg2}")
    _log_analysis_failure(test_state, market_analysis.symbol, error_msg2)
    
    # Check if log was created
    new_log2 = session.query(ActivityLog).filter(
        ActivityLog.type == ActivityLogType.ANALYSIS_FAILED,
        ActivityLog.source_expert_id == expert_instance.id
    ).order_by(ActivityLog.created_at.desc()).first()
    
    if new_log2 and new_log2.data.get('error_type') == "Analysis Error":
        print("   ✅ Activity log created!")
        print(f"   Error type: {new_log2.data.get('error_type')}")
    else:
        print("   ❌ Activity log not created or wrong type!")
    
    # Test 3: Missing market_analysis_id (should not log)
    print("\n3. Testing with missing market_analysis_id (should not log):")
    test_state_no_id = {
        "company_of_interest": "TSLA",
        "current_price": 100.0
    }
    
    logs_before = session.query(ActivityLog).filter(
        ActivityLog.type == ActivityLogType.ANALYSIS_FAILED
    ).count()
    
    _log_analysis_failure(test_state_no_id, "TSLA", "Some error")
    
    logs_after = session.query(ActivityLog).filter(
        ActivityLog.type == ActivityLogType.ANALYSIS_FAILED
    ).count()
    
    if logs_before == logs_after:
        print("   ✅ No log created (expected - standalone mode)")
    else:
        print("   ❌ Log was created (unexpected!)")
    
    print("\n" + "=" * 80)
    print("✅ All tests completed!")


if __name__ == "__main__":
    test_analysis_failure_logging()
