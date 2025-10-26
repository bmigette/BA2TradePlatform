#!/usr/bin/env python3
"""Check analysis 103 state and outputs"""

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import MarketAnalysis, AnalysisOutput
from sqlmodel import select
import json

with get_db() as session:
    # Get analysis 103
    analysis = session.get(MarketAnalysis, 103)
    
    if not analysis:
        print("Analysis 103 not found")
        exit(1)
    
    print(f"Analysis 103 found")
    print(f"Symbol: {analysis.symbol}")
    print(f"Status: {analysis.status}")
    print(f"Error: {getattr(analysis, 'error', None)}")
    print(f"Created: {analysis.created_at}")
    print()
    
    # Check state
    state = analysis.state if analysis.state else {}
    trading_state = state.get('trading_agent_graph', {}) if isinstance(state, dict) else {}
    
    print(f"Has trading_agent_graph: {bool(trading_state)}")
    print(f"Trading state keys: {list(trading_state.keys())}")
    print(f"Has final_trade_decision: {'final_trade_decision' in trading_state}")
    print()
    
    if 'final_trade_decision' in trading_state:
        decision = trading_state['final_trade_decision']
        print(f"Final trade decision content (first 500 chars):")
        print(f"{str(decision)[:500]}")
        print()
    
    # Check AnalysisOutput table
    outputs = session.exec(
        select(AnalysisOutput)
        .where(AnalysisOutput.market_analysis_id == 103)
    ).all()
    
    print(f"AnalysisOutput records: {len(outputs)}")
    for output in outputs:
        print(f"  - {output.name} (type: {output.type}, length: {len(output.text) if output.text else 0})")
    print()
    
    # Check if analysis completed successfully
    print(f"Analysis completed successfully: {analysis.status.value == 'completed' if hasattr(analysis.status, 'value') else analysis.status == 'completed'}")
