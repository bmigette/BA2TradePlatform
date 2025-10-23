"""
Test script for SmartRiskExpertInterface implementation in TradingAgents.

This script tests the refactored get_available_outputs and get_output_detail methods
to ensure they:
1. Return agent-level outputs matching UI tabs structure
2. Format debates with speaker indications (bull/bear, risky/safe/neutral)
3. Truncate large outputs at ~300K characters with <truncated> marker
4. Don't expose the full state (which is too large)
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import MarketAnalysis
from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents
from sqlmodel import select


def test_get_available_outputs():
    """Test that get_available_outputs returns agent-level outputs."""
    print("=" * 80)
    print("TEST: get_available_outputs()")
    print("=" * 80)
    
    # Find a completed TradingAgents analysis
    with get_db() as session:
        statement = (
            select(MarketAnalysis)
            .where(MarketAnalysis.status == 'COMPLETED')
            .where(MarketAnalysis.state.isnot(None))
            .order_by(MarketAnalysis.created_at.desc())
        )
        analyses = session.exec(statement).all()
        
        if not analyses:
            print("❌ No completed analyses found in database")
            return False
        
        # Get the first completed analysis
        analysis = analyses[0]
        print(f"✅ Found analysis ID {analysis.id} for {analysis.symbol}")
        
        # Get expert instance
        expert = TradingAgents(analysis.expert_instance_id)
        
        # Test get_available_outputs
        outputs = expert.get_available_outputs(analysis.id)
        
        print(f"\n📋 Available Outputs ({len(outputs)} total):")
        print("-" * 80)
        
        expected_keys = [
            'analysis_summary',
            'market_report',
            'sentiment_report',
            'news_report',
            'fundamentals_report',
            'macro_report',
            'investment_debate',
            'investment_plan',
            'trader_investment_plan',
            'risk_debate',
            'final_trade_decision'
        ]
        
        for key, description in outputs.items():
            icon = "✅" if key in expected_keys else "⚠️"
            print(f"{icon} {key}: {description}")
        
        # Verify no full state exposure
        if any('full_state' in key.lower() for key in outputs.keys()):
            print("\n❌ FAIL: Full state is exposed (should not be)")
            return False
        
        # Verify key outputs are present (at least some)
        found_keys = set(outputs.keys()) & set(expected_keys)
        if not found_keys:
            print("\n❌ FAIL: No expected output keys found")
            return False
        
        print(f"\n✅ PASS: Found {len(found_keys)} expected output types")
        print(f"   Keys: {', '.join(sorted(found_keys))}")
        
        return True, analysis.id, expert


def test_get_output_detail(analysis_id, expert):
    """Test that get_output_detail returns formatted content with truncation."""
    print("\n" + "=" * 80)
    print("TEST: get_output_detail()")
    print("=" * 80)
    
    outputs = expert.get_available_outputs(analysis_id)
    
    # Test a few key outputs
    test_keys = []
    
    # Add analyst reports if available
    for key in ['market_report', 'news_report', 'fundamentals_report']:
        if key in outputs:
            test_keys.append(key)
            break  # Just test one analyst report
    
    # Add debate if available
    if 'investment_debate' in outputs:
        test_keys.append('investment_debate')
    if 'risk_debate' in outputs:
        test_keys.append('risk_debate')
    
    # Add summary if available
    if 'analysis_summary' in outputs:
        test_keys.append('analysis_summary')
    
    if not test_keys:
        print("❌ No outputs available to test")
        return False
    
    print(f"\n📝 Testing {len(test_keys)} output types:")
    print("-" * 80)
    
    all_passed = True
    
    for output_key in test_keys:
        try:
            content = expert.get_output_detail(analysis_id, output_key)
            
            # Check content length
            content_len = len(content)
            is_truncated = content.endswith("<truncated>")
            
            # Verify debate formatting if it's a debate output
            has_speaker_indications = False
            if 'debate' in output_key:
                # Check for speaker markers
                bull_marker = "🐂 Bull Researcher" in content or "Bull Researcher" in content
                bear_marker = "🐻 Bear Researcher" in content or "Bear Researcher" in content
                risky_marker = "⚡ Risky Analyst" in content
                safe_marker = "🛡️ Safe Analyst" in content
                neutral_marker = "⚖️ Neutral Analyst" in content
                
                has_speaker_indications = (
                    (bull_marker or bear_marker) or
                    (risky_marker or safe_marker or neutral_marker)
                )
            
            # Print results
            status_icon = "✅"
            status_details = []
            
            if content_len > 300_000:
                status_icon = "❌"
                status_details.append(f"TOO LONG: {content_len:,} chars")
                all_passed = False
            else:
                status_details.append(f"{content_len:,} chars")
            
            if is_truncated:
                status_details.append("TRUNCATED")
            
            if 'debate' in output_key:
                if has_speaker_indications:
                    status_details.append("HAS SPEAKERS")
                else:
                    status_icon = "⚠️"
                    status_details.append("NO SPEAKERS")
                    all_passed = False
            
            print(f"{status_icon} {outputs[output_key]}")
            print(f"   {' | '.join(status_details)}")
            
            # Show preview (first 200 chars)
            preview = content[:200].replace('\n', ' ')
            if len(content) > 200:
                preview += "..."
            print(f"   Preview: {preview}")
            
        except KeyError as e:
            print(f"❌ {outputs[output_key]}: KeyError - {e}")
            all_passed = False
        except Exception as e:
            print(f"❌ {outputs[output_key]}: Error - {e}")
            all_passed = False
    
    if all_passed:
        print("\n✅ PASS: All outputs retrieved successfully")
    else:
        print("\n❌ FAIL: Some outputs had issues")
    
    return all_passed


def main():
    """Run all tests."""
    print("\n" + "=" * 80)
    print("SMART RISK EXPERT INTERFACE TESTS")
    print("Testing TradingAgents implementation")
    print("=" * 80)
    
    # Test 1: get_available_outputs
    result = test_get_available_outputs()
    if not result:
        print("\n❌ TEST SUITE FAILED: get_available_outputs test failed")
        return
    
    success, analysis_id, expert = result
    
    # Test 2: get_output_detail
    result = test_get_output_detail(analysis_id, expert)
    
    # Final summary
    print("\n" + "=" * 80)
    if result:
        print("✅ ALL TESTS PASSED")
    else:
        print("❌ SOME TESTS FAILED")
    print("=" * 80)


if __name__ == "__main__":
    main()
