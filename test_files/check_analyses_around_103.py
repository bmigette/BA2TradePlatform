#!/usr/bin/env python3
"""Check analyses around ID 103"""

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import MarketAnalysis
from sqlmodel import select

with get_db() as session:
    # Get analyses around ID 103
    analyses = session.exec(
        select(MarketAnalysis)
        .where(MarketAnalysis.id >= 95)
        .where(MarketAnalysis.id <= 115)
        .order_by(MarketAnalysis.id)
    ).all()
    
    print(f"Checking analyses 95-115:\n")
    
    for analysis in analyses:
        state = analysis.state if analysis.state else {}
        trading_state = state.get('trading_agent_graph', {}) if isinstance(state, dict) else {}
        has_decision = 'final_trade_decision' in trading_state
        
        status_str = analysis.status.value if hasattr(analysis.status, 'value') else str(analysis.status)
        
        print(f"ID {analysis.id:4d} | {analysis.symbol:6s} | {status_str:12s} | Has decision: {has_decision}")
