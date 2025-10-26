#!/usr/bin/env python3
"""Test that valid analyses with final_trade_decision are included"""

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ExpertInstance
from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from sqlmodel import select

with get_db() as session:
    # Get first TradingAgents expert instance
    expert_inst = session.exec(
        select(ExpertInstance)
        .where(ExpertInstance.expert == 'TradingAgents')
    ).first()
    
    if not expert_inst:
        print("No TradingAgents expert found")
        exit(1)
    
    print(f"Testing with expert instance {expert_inst.id}")
    print()
    
    # Create toolkit
    toolkit = SmartRiskManagerToolkit(expert_inst.id, expert_inst.account_id)
    
    # Test: Try to fetch final_trade_decision from recent analyses (should be included)
    print("Testing batch fetch with recent analyses 1880-1884 (should have final_trade_decision):")
    result = toolkit.get_analysis_outputs_batch(
        analysis_ids=[1880, 1881, 1882, 1883, 1884],
        output_keys=['final_trade_decision'],
        max_tokens=100000
    )
    
    print(f"Items included: {result['items_included']}")
    print(f"Items skipped: {result['items_skipped']}")
    print(f"Total chars: {result['total_chars']:,}")
    print()
    
    print("Outputs included:")
    for output in result['outputs']:
        print(f"  - Analysis {output['analysis_id']}: {output['output_key']}")
        print(f"    Content length: {len(output['content'])} chars")
        print(f"    Preview: {output['content'][:200]}...")
        print()
    
    if result['skipped_items']:
        print("Skipped items:")
        for item in result['skipped_items']:
            print(f"  - Analysis {item['analysis_id']}: {item['output_key']}")
            print(f"    Reason: {item['reason']}")
        print()
    
    # Verify: All should be included (or at least most if they're valid)
    if result['items_included'] >= 4:  # Allow for 1 potential error
        print(f"✅ SUCCESS: {result['items_included']}/5 analyses with final_trade_decision were included!")
    else:
        print(f"❌ FAILURE: Only {result['items_included']}/5 analyses were included!")
