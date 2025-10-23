"""
Test script for get_analysis_outputs_batch functionality.

This test demonstrates the new batch fetching capability that allows
fetching multiple analysis outputs in a single call with automatic
truncation handling.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import MarketAnalysis
from sqlmodel import select


def test_batch_outputs():
    """Test the batch output fetching functionality."""
    
    print("=" * 80)
    print("Testing get_analysis_outputs_batch functionality")
    print("=" * 80)
    
    # Find a test expert instance and account
    with get_db() as session:
        # Get some recent analyses
        analyses = session.exec(
            select(MarketAnalysis)
            .order_by(MarketAnalysis.created_at.desc())
            .limit(5)
        ).all()
        
        if not analyses:
            print("❌ No market analyses found in database")
            return
        
        print(f"\n✅ Found {len(analyses)} recent analyses")
        
        # Get expert_instance_id and account_id from first analysis
        expert_instance_id = analyses[0].expert_instance_id
        
        # Get account_id from expert instance
        from ba2_trade_platform.core.models import ExpertInstance
        expert_instance = session.get(ExpertInstance, expert_instance_id)
        if not expert_instance:
            print(f"❌ Expert instance {expert_instance_id} not found")
            return
        
        account_id = expert_instance.account_id
        
        print(f"\nUsing expert_instance_id: {expert_instance_id}")
        print(f"Using account_id: {account_id}")
        
        # Create toolkit
        print("\n" + "-" * 80)
        print("Creating SmartRiskManagerToolkit...")
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        print("✅ Toolkit created")
        
        # Test 1: Get available outputs for first analysis
        print("\n" + "-" * 80)
        print(f"Test 1: Get available outputs for analysis {analyses[0].id}")
        print("-" * 80)
        
        try:
            outputs = toolkit.get_analysis_outputs(analyses[0].id)
            print(f"✅ Found {len(outputs)} available outputs:")
            for key, desc in list(outputs.items())[:5]:  # Show first 5
                print(f"   - {key}: {desc}")
            if len(outputs) > 5:
                print(f"   ... and {len(outputs) - 5} more")
        except Exception as e:
            print(f"❌ Error: {e}")
            return
        
        # Test 2: Single analysis, multiple outputs (batch)
        print("\n" + "-" * 80)
        print(f"Test 2: Batch fetch multiple outputs from analysis {analyses[0].id}")
        print("-" * 80)
        
        # Get first 3 output keys
        output_keys = list(outputs.keys())[:3]
        print(f"Fetching outputs: {output_keys}")
        
        requests = [
            {
                "analysis_id": analyses[0].id,
                "output_keys": output_keys
            }
        ]
        
        try:
            result = toolkit.get_analysis_outputs_batch(requests, max_tokens=50000)
            
            print(f"\n📊 Batch Result:")
            print(f"   - Items included: {result['items_included']}")
            print(f"   - Items skipped: {result['items_skipped']}")
            print(f"   - Total chars: {result['total_chars']:,}")
            print(f"   - Estimated tokens: {result['total_tokens_estimate']:,}")
            print(f"   - Truncated: {result['truncated']}")
            
            if result['outputs']:
                print(f"\n✅ Successfully fetched {len(result['outputs'])} outputs:")
                for output in result['outputs']:
                    content_preview = output['content'][:100].replace('\n', ' ')
                    print(f"   - [{output['analysis_id']}] {output['output_key']}")
                    print(f"     Symbol: {output['symbol']}")
                    print(f"     Length: {output['included_length']:,} chars")
                    print(f"     Preview: {content_preview}...")
            
            if result['skipped_items']:
                print(f"\n⚠️ Skipped {len(result['skipped_items'])} items:")
                for item in result['skipped_items']:
                    print(f"   - [{item['analysis_id']}] {item['output_key']}: {item['reason']}")
        
        except Exception as e:
            print(f"❌ Error in batch fetch: {e}")
            import traceback
            traceback.print_exc()
            return
        
        # Test 3: Multiple analyses, multiple outputs (batch)
        if len(analyses) >= 3:
            print("\n" + "-" * 80)
            print("Test 3: Batch fetch from multiple analyses")
            print("-" * 80)
            
            # Get available outputs for each analysis
            requests = []
            for i, analysis in enumerate(analyses[:3]):
                try:
                    analysis_outputs = toolkit.get_analysis_outputs(analysis.id)
                    # Get first 2 output keys
                    keys = list(analysis_outputs.keys())[:2]
                    requests.append({
                        "analysis_id": analysis.id,
                        "output_keys": keys
                    })
                    print(f"   Analysis {analysis.id} ({analysis.symbol}): fetching {keys}")
                except Exception as e:
                    print(f"   Analysis {analysis.id}: skipped (error: {e})")
            
            if requests:
                try:
                    result = toolkit.get_analysis_outputs_batch(requests, max_tokens=50000)
                    
                    print(f"\n📊 Multi-Analysis Batch Result:")
                    print(f"   - Items included: {result['items_included']}")
                    print(f"   - Items skipped: {result['items_skipped']}")
                    print(f"   - Total chars: {result['total_chars']:,}")
                    print(f"   - Estimated tokens: {result['total_tokens_estimate']:,}")
                    print(f"   - Truncated: {result['truncated']}")
                    
                    if result['outputs']:
                        print(f"\n✅ Successfully fetched outputs from {len(set(o['analysis_id'] for o in result['outputs']))} analyses")
                
                except Exception as e:
                    print(f"❌ Error in multi-analysis batch: {e}")
        
        # Test 4: Truncation test (very low limit)
        print("\n" + "-" * 80)
        print("Test 4: Truncation handling (max_tokens=1000)")
        print("-" * 80)
        
        requests = [
            {
                "analysis_id": analyses[0].id,
                "output_keys": list(outputs.keys())[:5]  # Try to fetch 5 outputs
            }
        ]
        
        try:
            result = toolkit.get_analysis_outputs_batch(requests, max_tokens=1000)
            
            print(f"\n📊 Truncation Test Result:")
            print(f"   - Items included: {result['items_included']}")
            print(f"   - Items skipped: {result['items_skipped']}")
            print(f"   - Total chars: {result['total_chars']:,}")
            print(f"   - Truncated: {result['truncated']}")
            
            if result['truncated']:
                print(f"\n✅ Truncation handled correctly!")
                print(f"   - {result['items_included']} outputs included (partial or full)")
                print(f"   - {result['items_skipped']} outputs skipped due to size limit")
            
            if result['skipped_items']:
                print(f"\n   Skipped items:")
                for item in result['skipped_items']:
                    print(f"   - [{item['analysis_id']}] {item['output_key']}: {item['reason']}")
        
        except Exception as e:
            print(f"❌ Error in truncation test: {e}")
        
        print("\n" + "=" * 80)
        print("✅ All tests completed!")
        print("=" * 80)


if __name__ == "__main__":
    test_batch_outputs()
